"""The actual SDK keeps guarded identity across response loss and router downgrade."""

from collections.abc import Iterator
from dataclasses import replace
from functools import partial
from typing import Any
from unittest.mock import MagicMock

import httpx
import psycopg
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

import meiva
import meiva.agents
from base.api_contracts.agent_contract import GatewayUnavailable
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.net.tests.retry_waits import retry_waits as retry_waits
from gateway.agents import router as agent_router
from gateway.app import lifespan
from gateway.tests.agents.gateway_auth import without_credentials
from gateway.tests.lifecycle.creation.test_guarded_creation import PATH, SECRET
from gateway.tests.lifecycle.creation.test_guarded_creation import client as client
from meiva.gateway_client import spawn, use_client
from meiva.sdk_surface import agent_identity
from tests.fixtures.gateway_config import GatewayTestClient, gateway_test_client
from tests.path_scoped.gateway_tests import local_spawn_in_process as local_spawn_in_process


@pytest.fixture
def sdk_http() -> Iterator[MagicMock]:
    http = MagicMock()
    with use_client(http):
        yield http


def sdk(**kwargs: Any) -> int:
    args: dict[str, Any] = {
        "spawner": "user",
        "prompt": "one goal",
        "fork_from": None,
        "prompt_source": "user",
        "machine": "local-test",
        "idempotency_key": "sdk-intent",
    }
    return spawn(**(args | kwargs))


def httpx_response_of(response: Any) -> httpx.Response:
    # Starlette's TestClient uses httpx2; preserve the real ASGI result while
    # giving the SDK its production httpx response/error boundary.
    return httpx.Response(
        response.status_code,
        headers=dict(response.headers),
        content=response.content,
        request=httpx.Request(response.request.method, str(response.request.url)),
    )


def _older(current: FastAPI) -> FastAPI:
    older = FastAPI(
        lifespan=partial(
            lifespan, process=current.state.gateway_process, process_ownership="borrowed"
        ),
        middleware=current.user_middleware,
    )
    older.state.gateway_config = current.state.gateway_config
    older.state.gateway_process = current.state.gateway_process
    older.exception_handlers = current.exception_handlers.copy()
    routes = APIRouter()
    routes.routes = [
        route for route in agent_router.router.routes if getattr(route, "path", None) != PATH
    ]
    older.include_router(routes)
    return older


@pytest.mark.usefixtures("retry_waits")
def test_sdk_lost_response_downgrade_and_restore_one_birth(
    client: TestClient,
    db_conn: psycopg.Connection,
    sdk_http: MagicMock,
) -> None:
    target = client
    lose = True
    calls: list[tuple[str, dict[str, Any]]] = []

    def submit(path: str, **kwargs: Any) -> httpx.Response:
        nonlocal lose
        calls.append((path, kwargs))
        response = target.post(path, **kwargs)
        if lose:
            assert response.status_code in (200, 201), response.text
            raise httpx.ReadTimeout("committed response lost")
        return httpx_response_of(response)

    http = sdk_http
    http.post.side_effect = submit
    with pytest.raises(GatewayUnavailable):
        sdk()
    lost_attempts = len(calls)
    assert lost_attempts > 1
    lose = False
    row = db_conn.execute("SELECT id FROM agents").fetchone()
    assert row is not None
    original = row[0]
    assert isinstance(client.app, FastAPI)
    with gateway_test_client(
        _older(client.app),
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as old:
        target = old
        with pytest.raises(httpx.HTTPStatusError) as error:
            sdk()
        assert error.value.response.status_code in (404, 405)
    target = client
    assert sdk() == original
    assert len(calls) == lost_attempts + 2
    assert all(call == calls[0] for call in calls)
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM audit_events WHERE event_name='spawn'"
    ).fetchone() == (1,)
    assert db_conn.execute("SELECT count(*) FROM inbound_messages").fetchone() == (1,)
    with pytest.raises(httpx.HTTPStatusError) as conflict:
        sdk(spawner="agent:999")
    assert conflict.value.response.status_code == 409
    with pytest.raises(httpx.HTTPStatusError) as changed:
        sdk(prompt="changed")
    assert changed.value.response.status_code == 409
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)
    http.get.assert_not_called()


def test_old_router_never_accepts_or_falls_back(
    client: TestClient,
    db_conn: psycopg.Connection,
    sdk_http: MagicMock,
) -> None:
    assert isinstance(client.app, FastAPI)
    with gateway_test_client(
        _older(client.app),
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as old:
        http = sdk_http

        def submit(path: str, **kwargs: Any) -> httpx.Response:
            return httpx_response_of(old.post(path, **kwargs))

        http.post.side_effect = submit
        with pytest.raises(httpx.HTTPStatusError):
            sdk()
        assert http.post.call_count == 1
        assert http.post.call_args.args == (PATH,)
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (0,)


def test_bearer_and_cookie_share_principal_but_revocation_blocks_replay(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    sdk_http: MagicMock,
) -> None:
    http = sdk_http
    active = client

    def submit(path: str, **kwargs: Any) -> httpx.Response:
        return httpx_response_of(active.post(path, **kwargs))

    http.post.side_effect = submit
    original = sdk()
    login = client.post("/api/auth/login", json={"password": SECRET})
    assert login.status_code == 200
    client.headers.pop("Authorization")
    assert sdk() == original
    with gateway_test_client(
        process=client.app.state.gateway_process,
        config=replace(
            client.app.state.gateway_config,
            cluster_secret="rotated-sdk-strong-secret",  # noqa: S106 -- isolated test credential
        ),
        cookies=dict(client.cookies),
    ) as rotated:
        without_credentials(rotated)
        active = rotated
        with pytest.raises(httpx.HTTPStatusError) as revoked:
            sdk()
    assert revoked.value.response.status_code == 401
    assert db_conn.execute("SELECT count(*) FROM agents").fetchone() == (1,)


def test_changed_sdk_context_same_principal_key_conflicts(
    client: TestClient,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    sdk_http: MagicMock,
) -> None:
    http = sdk_http

    def submit(path: str, **kwargs: Any) -> httpx.Response:
        return httpx_response_of(client.post(path, **kwargs))

    http.post.side_effect = submit
    monkeypatch.setattr(agent_identity, "require_actor", lambda: "user")
    meiva.context.require_machine().set(name="local-test")
    original = meiva.agents.spawn(prompt="same raw inputs", idempotency_key="context")
    meiva.context.require_machine().set(name="another-machine")
    with pytest.raises(httpx.HTTPStatusError) as changed:
        meiva.agents.spawn(prompt="same raw inputs", idempotency_key="context")
    assert changed.value.response.status_code == 409
    assert db_conn.execute("SELECT id FROM agents").fetchall() == [(original,)]
    assert {call.args[0] for call in http.post.call_args_list} == {PATH}
