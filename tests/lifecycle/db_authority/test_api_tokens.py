"""Machine API tokens of the write generation: the token acceptance matrix.

The generation's secret carries one API token per class. The gateway (API
middleware, bootstrap, webhooks, managed-browser login) accepts the human
cluster secret or the ACTIVE generation's tokens; a runner's ops server
accepts the generation's two tokens and never the human secret; the launcher
delivers each service its class token only while the API is authenticated;
clients present the delivered token first. The ledger side needs no database;
the gateway checks run the real app against the suite's database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from base.cluster import authority
from base.cluster.auth import bearer_header, cookie_name
from base.cluster.authority import api, ledger, unit
from base.cluster.machine import MachineRole, gateway_auth_headers
from base.config import settings
from base.daemon.http_transport import start_daemon_http
from cli.commands.data_plane import bringup
from gateway.auth import request_principal
from gateway.auth.request_principal import cluster_credential
from gateway.auth.webhook import authenticate_webhook
from ops.roster.service_spec import ServiceSpec, api_access
from services.agent_ops import _boot as ops_boot

_HUMAN = "human-" + "h" * 40
_ENDPOINT = "postgresql://ava@10.0.0.7:6433/ava"


def _tokens(home: Path) -> authority.GenerationSecret:
    return authority.read_secret(home, authority.active_generation(home))


def _unadmit(home: Path) -> None:
    """The state of a birth that has minted its generation but not admitted it:
    the generation is pending, nothing is active."""
    current = ledger.require_ledger(home)
    ledger._write(home, ledger._replace(current, active=None, pending=current.active))


@pytest.fixture
def gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed_write_generation: Callable[[Path], Any]
) -> Path:
    """A gateway home with an active generation 0, as this process's home, with
    its human secret set (an authenticated API)."""
    home = (tmp_path / "gateway").resolve()
    home.mkdir(mode=0o700)
    seed_write_generation(home)
    # The gateway's bootstrap serves the connection facts its home's `.env` declares.
    (home / ".env").write_text(
        f"AVA_DB_URL={settings.data_plane.db_url}\nAVA_REDIS_URL={settings.data_plane.redis_url}\n"
    )
    monkeypatch.setenv("AVA_HOME", str(home))
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _HUMAN)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    return home


def _bearer(token: str) -> str:
    return f"Bearer {token}"


# ── the generation's tokens and the gateway's acceptance ────────────────────


def test_the_generation_mints_distinct_class_tokens(gateway: Path) -> None:
    tokens = _tokens(gateway).api
    assert len({tokens.gateway, tokens.runner}) == 2
    assert api.acceptance(gateway) == {
        "gateway": api.token_digest(tokens.gateway),
        "runner": api.token_digest(tokens.runner),
    }
    # Admission rewrites the ledger: the cached acceptance follows it.
    _unadmit(gateway)
    assert api.acceptance(gateway) == {}


def test_no_ledger_or_no_active_generation_accepts_no_machine_token(
    tmp_path: Path, gateway: Path
) -> None:
    bare = (tmp_path / "bare").resolve()
    bare.mkdir(mode=0o700)
    assert api.acceptance(bare) == {}
    _unadmit(gateway)  # a pending generation is never accepted
    assert api.acceptance(gateway) == {}


def test_cluster_credential_names_the_verifying_credential(gateway: Path) -> None:
    tokens = _tokens(gateway).api
    assert cluster_credential(_bearer(_HUMAN), _HUMAN) == "cluster_bearer"
    assert cluster_credential(_bearer(tokens.gateway), _HUMAN) == "machine_token:gateway"
    assert cluster_credential(_bearer(tokens.runner), _HUMAN) == "machine_token:runner"
    for refused in (
        None,
        "",
        tokens.runner,  # no scheme
        _bearer("x" * 43),
        _bearer(api.token_digest(tokens.runner)),  # a digest is not a bearer
        _bearer(""),
    ):
        assert cluster_credential(refused, _HUMAN) is None
    _unadmit(gateway)
    assert cluster_credential(_bearer(tokens.gateway), _HUMAN) is None
    assert cluster_credential(_bearer(tokens.runner), _HUMAN) is None
    # A blank human secret never verifies as a bearer.
    assert cluster_credential(_bearer(""), "") is None


# ── the gateway app: middleware, bootstrap, login ───────────────────────────


def test_gateway_api_admits_the_active_generation_and_the_human_secret(gateway: Path) -> None:
    before = _tokens(gateway).api
    with TestClient(config_app()) as client:
        for token in (_HUMAN, before.gateway, before.runner):
            assert client.get("/api/agents", headers=bearer_header(token)).status_code == 200
        assert client.get("/api/agents").status_code == 401
        assert client.get("/api/agents", headers=bearer_header("x" * 43)).status_code == 401
        _unadmit(gateway)
        for refused in (before.gateway, before.runner):
            assert client.get("/api/agents", headers=bearer_header(refused)).status_code == 401
        assert client.get("/api/agents", headers=bearer_header(_HUMAN)).status_code == 200


def test_an_open_api_ignores_bearers(gateway: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    with TestClient(config_app()) as client:
        assert client.get("/api/agents").status_code == 200


def test_bootstrap_serves_a_runner_token_but_never_the_human_secret(gateway: Path) -> None:
    runner = _tokens(gateway).api.runner
    with TestClient(config_app()) as client:
        served = client.get("/api/bootstrap", headers=bearer_header(runner))
        assert served.status_code == 200
        payload = served.json()
        assert "AVA_CLUSTER_SECRET" not in payload
        assert _HUMAN not in json.dumps(payload)
        _unadmit(gateway)
        assert client.get("/api/bootstrap", headers=bearer_header(runner)).status_code == 401


def _login(client: TestClient, password: str) -> int:
    return client.post("/api/auth/login", json={"password": password}).status_code


def test_login_admits_the_active_runner_token_for_the_managed_browser(gateway: Path) -> None:
    tokens = _tokens(gateway).api
    with TestClient(config_app()) as client:
        assert _login(client, tokens.gateway) == 401
        response = client.post("/api/auth/login", json={"password": tokens.runner})
        assert response.status_code == 200 and response.cookies[cookie_name()]
        _unadmit(gateway)
        assert _login(client, tokens.runner) == 401


def _cookie_authenticates(client: TestClient, cookie: str) -> tuple[int, bool]:
    """(`GET /api/agents` status, `/api/auth/check` verdict) presenting only `cookie`."""
    client.cookies.clear()
    headers = {"Cookie": f"{cookie_name()}={cookie}"}
    agents = client.get("/api/agents", headers=headers).status_code
    check = client.get("/api/auth/check", headers=headers).json()["authenticated"]
    return agents, check


def test_a_human_minted_session_dies_with_the_rotated_secret(
    gateway: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser logged in with the human secret stays authenticated while the secret
    holds, but not past a rotation of the secret itself: the restarted gateway admits
    only sessions its current secret minted."""
    with TestClient(config_app()) as client:
        cookie = client.post("/api/auth/login", json={"password": _HUMAN}).cookies[cookie_name()]
        assert _cookie_authenticates(client, cookie) == (200, True)
        monkeypatch.setattr(settings.data_plane, "cluster_secret", "rotated-" + "r" * 40)
        assert _cookie_authenticates(client, cookie) == (401, False)


def test_a_session_cookie_names_its_credential_only_through_a_keyed_mac(gateway: Path) -> None:
    """The cookie travels to the browser, so the mint in it must not let its
    holder brute-force the human secret offline: it is an HMAC under the
    gateway's private session key, never a plain digest of the credential."""
    with TestClient(config_app()) as client:
        cookie = client.post("/api/auth/login", json={"password": _HUMAN}).cookies[cookie_name()]
        mint, _, _random = cookie.partition(".")
        assert mint.startswith("human-")
        digest = api.token_digest(_HUMAN)
        unkeyed = hashlib.sha256(f"human\0{digest}".encode()).hexdigest()
        for leaked in (_HUMAN, digest, unkeyed, hashlib.sha256(digest.encode()).hexdigest()):
            assert leaked[:16] not in cookie
        key = request_principal.session_key_path(gateway)
        assert key.stat().st_mode & 0o777 == 0o600
        # The mint depends on the key: a new key mints the same credential
        # differently and ends every session the old key bound.
        key.unlink()
        renewed = client.post("/api/auth/login", json={"password": _HUMAN}).cookies[cookie_name()]
        assert renewed.partition(".")[0] not in (mint, "")
        assert _cookie_authenticates(client, cookie) == (401, False)
        assert _cookie_authenticates(client, renewed) == (200, True)


def test_a_session_records_which_credential_minted_it(gateway: Path) -> None:
    """Inbound provenance tells a machine-minted browser session from a human one."""
    runner = _tokens(gateway).api.runner
    with TestClient(config_app()) as client:
        human = client.post("/api/auth/login", json={"password": _HUMAN}).cookies[cookie_name()]
        client.cookies.clear()
        machine = client.post("/api/auth/login", json={"password": runner}).cookies[cookie_name()]
        pool = client.app.state.db_pool  # type: ignore[attr-defined]
        assert request_principal.current_session_fact(pool, human, _HUMAN) == "user_session"
        assert (
            request_principal.current_session_fact(pool, machine, _HUMAN)
            == "machine_session:runner"
        )


def test_the_sessions_list_shows_only_sessions_that_authenticate(gateway: Path) -> None:
    """`/api/auth/sessions` lists what the session check would still admit: an id
    without a mint (the pre-mint format) never appears."""
    from base.cluster.auth import new_session_id
    from gateway.auth.session_store import create_session

    runner = _tokens(gateway).api.runner
    with TestClient(config_app()) as client:
        pool = client.app.state.db_pool  # type: ignore[attr-defined]
        machine = client.post("/api/auth/login", json={"password": runner}).cookies[cookie_name()]
        client.cookies.clear()
        human = client.post("/api/auth/login", json={"password": _HUMAN}).cookies[cookie_name()]
        client.cookies.clear()
        create_session(pool, new_session_id(), 3600, "legacy-browser", "10.0.0.9")

        def listed() -> set[str]:
            response = client.get("/api/auth/sessions", headers=bearer_header(_HUMAN))
            assert response.status_code == 200, response.text
            return {row["id"] for row in response.json()}

        assert listed() == {machine[-8:], human[-8:]}


def test_a_machine_token_cannot_choose_the_human_secret(
    gateway: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner token authenticates the administrator, but it must not turn a
    generation-bound admission into a human bearer of its choosing."""
    from base.host.env import runtime_config

    store = tmp_path / "config-store"
    store.mkdir()
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: store)
    monkeypatch.setattr(settings.general, "machine_name", "gateway-host")
    runner = _tokens(gateway).api.runner
    with TestClient(config_app()) as client:
        resp = client.put(
            "/api/config",
            json={"cluster_secret": "chosen-" + "c" * 40},
            headers=bearer_header(runner),
        )
    assert resp.status_code == 400, resp.text
    assert not (store / ".env").exists()


def test_a_machine_token_cannot_open_the_mcp_endpoint(
    gateway: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching `/mcp` on decides whether MCP client tokens, which outlive
    every generation, authenticate at all: no config write may choose it."""
    from base.host.env import runtime_config

    store = tmp_path / "config-store"
    store.mkdir()
    monkeypatch.setattr(runtime_config, "_ava_home", lambda: store)
    monkeypatch.setattr(settings.general, "machine_name", "gateway-host")
    runner = _tokens(gateway).api.runner
    with TestClient(config_app()) as client:
        resp = client.put(
            "/api/config", json={"mcp_endpoint_enabled": True}, headers=bearer_header(runner)
        )
    assert resp.status_code == 400, resp.text
    assert not (store / ".env").exists()


def test_only_a_human_credential_manages_mcp_clients(gateway: Path) -> None:
    """An MCP client token lives in `mcp_clients`, bound to no write generation.
    A generation-bound admission (either machine token, or a session a runner
    token minted) must not mint one that outlives it, nor list or revoke them;
    the human secret and the sessions it minted manage them."""
    from gateway.mcp_server import clients

    tokens = _tokens(gateway).api
    with TestClient(config_app()) as client:
        pool = client.app.state.db_pool  # type: ignore[attr-defined]
        runner_session = client.post("/api/auth/login", json={"password": tokens.runner})
        client.cookies.clear()
        human_session = client.post("/api/auth/login", json={"password": _HUMAN})
        client.cookies.clear()
        generation_bound = (
            bearer_header(tokens.runner),
            bearer_header(tokens.gateway),
            {"Cookie": f"{cookie_name()}={runner_session.cookies[cookie_name()]}"},
        )
        for headers in generation_bound:
            minted = client.post(
                "/api/mcp/clients", json={"name": "minted", "scope": "write"}, headers=headers
            )
            assert minted.status_code == 403, minted.text
            assert client.get("/api/mcp/clients", headers=headers).status_code == 403
            assert client.post("/api/mcp/clients/1/revoke", headers=headers).status_code == 403
        assert clients.list_clients(pool) == []

        human = (
            bearer_header(_HUMAN),
            {"Cookie": f"{cookie_name()}={human_session.cookies[cookie_name()]}"},
        )
        for index, headers in enumerate(human):
            created = client.post(
                "/api/mcp/clients",
                json={"name": f"human-{index}", "scope": "write"},
                headers=headers,
            )
            assert created.status_code == 200, created.text
            assert client.get("/api/mcp/clients", headers=headers).status_code == 200
            revoked = client.post(
                f"/api/mcp/clients/{created.json()['id']}/revoke", headers=headers
            )
            assert revoked.status_code == 200, revoked.text


def _webhook_request(authorization: str | None) -> Request:
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    return Request({"type": "http", "headers": headers, "client": ("10.0.0.9", 5000)})


def test_webhooks_admit_the_active_generation_off_loopback(gateway: Path) -> None:
    runner = _tokens(gateway).api.runner
    admitted = authenticate_webhook(_webhook_request(_bearer(runner)), provider="alerts")
    assert admitted == (True, "machine_token:runner")
    _unadmit(gateway)
    assert not authenticate_webhook(_webhook_request(_bearer(runner)), provider="alerts")[0]


def config_app() -> Any:
    from gateway.app import app

    return app


# ── the runner's ops server ─────────────────────────────────────────────────


async def _ops_status(digests: frozenset[str] | None, bearer: str | None) -> int:
    async def route(_body: bytes) -> tuple[int, bytes, str]:
        return 200, b"{}", "application/json"

    server = await start_daemon_http(
        host="127.0.0.1",
        port=0,
        health_response=lambda: (200, b"{}"),
        extra_routes={("POST", "/ops"): route},
        auth_digests=digests,
    )
    port = server.sockets[0].getsockname()[1]
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        auth = "" if bearer is None else f"Authorization: Bearer {bearer}\r\n"
        writer.write(f"POST /ops HTTP/1.1\r\n{auth}Content-Length: 2\r\n\r\n{{}}".encode())
        await writer.drain()
        status = int((await reader.readline()).split(b" ")[1])
        writer.close()
        await writer.wait_closed()
        return status
    finally:
        server.close()
        await server.wait_closed()


def test_gateway_home_ops_accepts_its_generation_never_the_human_secret(gateway: Path) -> None:
    tokens = _tokens(gateway).api
    acceptance = ops_boot._ops_acceptance()
    assert acceptance == frozenset(api.acceptance(gateway).values())
    assert ops_boot._ops_bind_host(acceptance) == "0.0.0.0"  # noqa: S104
    for token, status in (
        (tokens.gateway, 200),
        (tokens.runner, 200),
        (_HUMAN, 401),
        (None, 401),
        ("x" * 43, 401),
        (api.token_digest(tokens.gateway), 401),  # the server's digest is not a bearer
    ):
        assert asyncio.run(_ops_status(acceptance, token)) == status
    _unadmit(gateway)
    assert asyncio.run(_ops_status(ops_boot._ops_acceptance(), tokens.gateway)) == 401


def test_gateway_home_ops_fails_closed_while_no_generation_is_active(gateway: Path) -> None:
    """Before a birth admits its generation none is active: a secret gateway home's
    /ops then accepts no bearer at all, and still binds as an authenticated server.
    It never falls back to the open, unauthenticated posture (None)."""
    tokens = _tokens(gateway).api
    _unadmit(gateway)
    acceptance = ops_boot._ops_acceptance()
    assert acceptance == frozenset()
    assert ops_boot._ops_bind_host(acceptance) == "0.0.0.0"  # noqa: S104
    for token in (tokens.gateway, tokens.runner, _HUMAN, None):
        assert asyncio.run(_ops_status(acceptance, token)) == 401


def test_ops_posture_follows_the_api(gateway: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    assert ops_boot._ops_acceptance() is None
    assert ops_boot._ops_bind_host(None) == "127.0.0.1"
    # A remote-managed plane keeps no generations: its gateway presents the human secret.
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _HUMAN)
    monkeypatch.setattr(type(settings.data_plane), "is_remote", property(lambda _self: True))
    assert ops_boot._ops_acceptance() == frozenset({api.token_digest(_HUMAN)})


@pytest.fixture
def runner_home(tmp_path: Path, gateway: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A remote unit home holding an API-bearing capability, as this process's
    home; it holds no human secret."""
    home = (tmp_path / "runner").resolve()
    home.mkdir(mode=0o700)
    issued = unit.issue_bundle(
        gateway,
        unit=unit.UnitIdentity(machine="mini", home=str(home)),
        endpoint=_ENDPOINT,
        cluster_secret=_HUMAN,
        ttl_s=600,
    )
    unit.install_bundle(
        home,
        unit.open_bundle(issued.envelope, issued.transport_key),
        machine="mini",
        served_endpoint=_ENDPOINT,
        probe=lambda _dsn: None,
    )
    monkeypatch.setenv("AVA_HOME", str(home))
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    return home


def test_the_unit_capability_carries_the_api_admission(gateway: Path, runner_home: Path) -> None:
    tokens = _tokens(gateway).api
    capability = unit.require_unit_capability(runner_home)
    assert capability.api == unit.UnitApi(
        token=tokens.runner,
        gateway=api.token_digest(tokens.gateway),
        telemetry=api.telemetry_token(_HUMAN),
    )
    # The unit holds neither the gateway token nor the human secret.
    stored = unit.unit_capability_path(runner_home).read_text()
    assert tokens.gateway not in stored and _HUMAN not in stored
    assert unit.telemetry_bearer(runner_home, "") == api.telemetry_token(_HUMAN)
    assert unit.unit_api_delivery(runner_home) == {api.API_TOKEN_ENV: tokens.runner}


def test_remote_unit_ops_accepts_its_generation_never_the_human_secret(
    gateway: Path, runner_home: Path
) -> None:
    tokens = _tokens(gateway).api
    acceptance = ops_boot._ops_acceptance()
    assert acceptance == frozenset(
        {api.token_digest(tokens.gateway), api.token_digest(tokens.runner)}
    )
    for token, status in ((tokens.gateway, 200), (tokens.runner, 200), (_HUMAN, 401)):
        assert asyncio.run(_ops_status(acceptance, token)) == status


def test_an_open_clusters_bundle_carries_no_api_admission(gateway: Path, tmp_path: Path) -> None:
    home = (tmp_path / "open-runner").resolve()
    home.mkdir(mode=0o700)
    issued = unit.issue_bundle(
        gateway,
        unit=unit.UnitIdentity(machine="mini", home=str(home)),
        endpoint=_ENDPOINT,
        cluster_secret="",
        ttl_s=600,
    )
    bundle = unit.open_bundle(issued.envelope, issued.transport_key)
    assert bundle.capability.api is None
    unit.install_bundle(
        home, bundle, machine="mini", served_endpoint=_ENDPOINT, probe=lambda _dsn: None
    )
    assert unit.unit_api_delivery(home) == {}
    assert unit.telemetry_bearer(home, "") is None


# ── launch delivery and clients ─────────────────────────────────────────────


def test_launch_delivers_the_class_token_only_while_authenticated(
    gateway: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = _tokens(gateway).api
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", lambda: True)
    assert bringup.api_delivery("gateway") == {api.API_TOKEN_ENV: tokens.gateway}
    assert bringup.api_delivery("runner") == {api.API_TOKEN_ENV: tokens.runner}
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    assert bringup.api_delivery("runner") == {}


def test_a_pure_runner_launch_delivers_its_capability_token(
    gateway: Path, runner_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", lambda: False)
    assert bringup.api_delivery("runner") == {api.API_TOKEN_ENV: _tokens(gateway).api.runner}
    with pytest.raises(RuntimeError, match="cannot launch a gateway-class"):
        bringup.api_delivery("gateway")


def test_api_class_follows_the_login_class_else_the_single_capability() -> None:
    gw: frozenset[MachineRole] = frozenset({"gateway"})
    runner: frozenset[MachineRole] = frozenset({"agent-runner"})
    both = gw | runner
    assert api_access(ServiceSpec(session="g", cmd="x", capabilities=gw, requires_db=False)) == (
        "gateway"
    )
    assert api_access(
        ServiceSpec(session="r", cmd="x", capabilities=runner, requires_db=False)
    ) == ("runner")
    assert (
        api_access(
            ServiceSpec(
                session="h", cmd="x", capabilities=runner, requires_db=True, profile="agent"
            )
        )
        == "runner"
    )
    assert (
        api_access(ServiceSpec(session="o", cmd="x", capabilities=both, requires_db=False)) is None
    )
    assert (
        api_access(
            ServiceSpec(
                session="d", cmd="x", capabilities=both, requires_db=True, db_access="gateway"
            )
        )
        == "gateway"
    )


def test_clients_present_the_delivered_token_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _HUMAN)
    monkeypatch.delitem(os.environ, api.API_TOKEN_ENV, raising=False)
    assert gateway_auth_headers() == bearer_header(_HUMAN)
    monkeypatch.setitem(os.environ, api.API_TOKEN_ENV, "delivered-" + "t" * 32)
    assert gateway_auth_headers() == bearer_header("delivered-" + "t" * 32)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    monkeypatch.delitem(os.environ, api.API_TOKEN_ENV)
    assert gateway_auth_headers() == {}


def test_the_telemetry_token_is_one_way_and_stable_per_secret() -> None:
    token = api.telemetry_token(_HUMAN)
    assert token == api.telemetry_token(_HUMAN) != api.telemetry_token(_HUMAN + "x")
    assert _HUMAN not in token and token != hashlib.sha256(_HUMAN.encode()).hexdigest()
    with pytest.raises(ValueError, match="open cluster"):
        api.telemetry_token("")


@pytest.fixture(autouse=True)
def _fresh_acceptance_cache() -> Iterator[None]:
    api._acceptance_cache.clear()
    yield
    api._acceptance_cache.clear()
