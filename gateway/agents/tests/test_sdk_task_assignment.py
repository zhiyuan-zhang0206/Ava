"""Compound SDK acceptance has one keyed route and no separate spawn/create recipe."""

from typing import Any
from unittest.mock import MagicMock

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ava.gateway_client import transport
from ava_builtins.plugins.ava_fleet import task_registry
from base.agents import GatewayUnavailable
from base.agents.context.identity import ExternalLease
from base.agents.tasks.model import Task
from gateway.agents import router as agent_router
from gateway.agents.tests.test_task_assignments import HEADERS, PATH, SECRET
from gateway.agents.tests.test_task_assignments import body as body
from gateway.agents.tests.test_task_assignments import client as client
from gateway.app import app
from tests.fixtures.gateway_config import gateway_test_client
from tests.fixtures.pin_agent import pin_agent


def _response(response: Any) -> httpx.Response:
    return httpx.Response(
        response.status_code,
        content=response.content,
        headers=dict(response.headers),
        request=httpx.Request(response.request.method, str(response.request.url)),
    )


def _sdk(body: dict[str, Any], **overrides: Any) -> tuple[Task, int]:
    pin_agent(body["actor_agent_id"])
    return task_registry.create_and_assign(
        body["task"]["title"],
        body["task"]["description"],
        parent=body["task"]["parent"],
        preset=overrides.pop("preset", "coder"),
        machine="local-test",
        operation_key="sdk-compound",
        **overrides,
    )


def test_real_sdk_response_loss_keeps_exact_body_path_key_and_pair(
    client: TestClient, db_conn: psycopg.Connection, body: dict[str, Any]
) -> None:
    http = MagicMock()
    captured: list[tuple[str, dict[str, Any], dict[str, str]]] = []
    original: dict[str, Any] | None = None

    def submit(path: str, **kwargs: Any) -> httpx.Response:
        nonlocal original
        captured.append((path, kwargs["json"], kwargs["headers"]))
        response = client.post(path, json=kwargs["json"], headers=kwargs["headers"])
        assert response.status_code == 201, response.text
        original = original or response.json()
        if len(captured) == 1:
            raise httpx.ReadTimeout("committed response lost", request=httpx.Request("POST", path))
        return _response(response)

    http.post.side_effect = submit
    with transport.use_client(http):
        with pytest.raises(GatewayUnavailable):
            _sdk(body)
        assert len(captured) == 1
        task, agent_id = _sdk(body)
    assert original is not None
    assert task.id == original["task"]["id"] and agent_id == original["agent_id"]
    assert captured[0] == captured[1]
    assert captured[0][0] == PATH
    assert captured[0][2] == {**HEADERS, "Idempotency-Key": "sdk-compound"}
    assert db_conn.execute("SELECT count(*) FROM task_assignment_receipts").fetchone() == (1,)


def test_unavailable_atomic_route_refuses_without_separate_spawn(body: dict[str, Any]) -> None:
    unavailable = FastAPI(lifespan=app.router.lifespan_context, middleware=app.user_middleware)
    unavailable.exception_handlers = app.exception_handlers.copy()
    unavailable.include_router(agent_router.router)
    http = MagicMock()
    with gateway_test_client(
        unavailable, headers={"Authorization": f"Bearer {SECRET}"}
    ) as endpoint:

        def submit(path: str, **kwargs: Any) -> httpx.Response:
            return _response(endpoint.post(path, json=kwargs["json"], headers=kwargs["headers"]))

        http.post.side_effect = submit
        with transport.use_client(http), pytest.raises(httpx.HTTPStatusError):
            _sdk(body)
    assert http.post.call_count == 1
    assert http.post.call_args.args == (PATH,)


@pytest.mark.parametrize("active", [False, True])
def test_borrowed_lease_is_rejected_before_http_or_validation(
    body: dict[str, Any], active: bool
) -> None:
    validated = MagicMock(return_value=body["actor_agent_id"])
    if not active:
        validated.side_effect = RuntimeError("lease expired")
    pin_agent(
        body["actor_agent_id"], lease=ExternalLease(body["actor_agent_id"], validated, lambda: None)
    )
    http = MagicMock()
    with transport.use_client(http), pytest.raises(ValueError, match="borrowed lease"):
        task_registry.create_and_assign(
            "X",
            "Y",
            parent=body["task"]["parent"],
            operation_key="sdk-compound",
        )
    http.post.assert_not_called()
    validated.assert_not_called()


@pytest.mark.parametrize(
    "kwargs,error",
    [
        ({}, TypeError),
        ({"operation_key": None}, TypeError),
        ({"operation_key": ""}, ValueError),
        ({"operation_key": "x" * 129}, ValueError),
        ({"operation_key": True}, TypeError),
        ({"operation_key": "x", "require_idempotency": True}, TypeError),
        ({"operation_key": "x", "require_idempotency": False}, TypeError),
        (
            {"operation_key": "x", "preset": "coder", "config_overlay": {"preset": "coder"}},
            ValueError,
        ),
    ],
)
def test_missing_invalid_key_and_retired_flag_fail_before_http(
    body: dict[str, Any], kwargs: dict[str, Any], error: type[Exception]
) -> None:
    pin_agent(body["actor_agent_id"])
    http = MagicMock()
    with transport.use_client(http), pytest.raises(error):
        task_registry.create_and_assign("X", "Y", parent=body["task"]["parent"], **kwargs)
    http.post.assert_not_called()


def test_same_principal_key_actor_drift_conflicts(
    client: TestClient, body: dict[str, Any], db_conn: psycopg.Connection
) -> None:
    http = MagicMock()

    def submit(path: str, **kwargs: Any) -> httpx.Response:
        return _response(client.post(path, json=kwargs["json"], headers=kwargs["headers"]))

    http.post.side_effect = submit
    with transport.use_client(http):
        original = _sdk(body)
        another = db_conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
        assert another is not None
        db_conn.commit()
        with pytest.raises(httpx.HTTPStatusError) as changed:
            _sdk({**body, "actor_agent_id": another[0]})
        assert changed.value.response.status_code == 409
        assert _sdk(body) == original
    assert db_conn.execute("SELECT count(*) FROM task_assignment_receipts").fetchone() == (1,)


@pytest.mark.parametrize("preset_in_overlay", [False, True])
def test_assignment_uses_only_the_requested_preset(
    client: TestClient, body: dict[str, Any], db_conn: psycopg.Connection, preset_in_overlay: bool
) -> None:
    http = MagicMock()
    submitted: list[dict[str, Any]] = []

    def submit(path: str, **kwargs: Any) -> httpx.Response:
        submitted.append(kwargs["json"])
        response = client.post(path, json=kwargs["json"], headers=kwargs["headers"])
        assert response.status_code == 201, response.text
        return _response(response)

    http.post.side_effect = submit
    with transport.use_client(http):
        task, agent_id = _sdk(
            body, preset=None, config_overlay={"preset": "coder"} if preset_in_overlay else None
        )
    assert task.owner == agent_id
    assert submitted[0]["agent"]["config"] == ({"preset": "coder"} if preset_in_overlay else {})
    assert db_conn.execute(
        "SELECT preset_name FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("coder" if preset_in_overlay else None,)
