"""Machine API tokens per write generation: the token acceptance matrix.

Every generation's secret carries one API token per class. The gateway (API
middleware, bootstrap, webhooks, managed-browser login) accepts the human
cluster secret or the ACTIVE generation's tokens; a runner's ops server
accepts its generation's two tokens and never the human secret; the launcher
delivers each service its class token only while the API is authenticated;
clients present the delivered token first. The ledger side needs no database;
the gateway checks run the real app against the suite's database.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from cli.commands.data_plane import bringup
from gateway import request_principal
from gateway.request_principal import cluster_credential
from gateway.routers._webhook_auth import authenticate_webhook
from ops.service_spec import ServiceSpec, api_access
from services.agent_ops import _boot as ops_boot
from shared.cluster import authority
from shared.cluster.authority import api, ledger, unit
from shared.cluster_auth import bearer_header, client_bearer, cookie_name
from shared.config import settings
from shared.daemon_http import start_daemon_http
from shared.machine import MachineRole, gateway_auth_headers

_HUMAN = "human-" + "h" * 40
_ENDPOINT = "postgresql://ava@10.0.0.7:6433/ava"


def _encrypt(name: str, _password: str) -> str:
    return f"SCRAM-SHA-256$4096:c2VlZA==${name}"


def _tokens(home: Path) -> authority.GenerationSecret:
    return authority.read_secret(home, authority.active_generation(home))


def _rotate(home: Path) -> None:
    """One release rotation of the ledger (the catalog side is proven elsewhere):
    revoke and close the active generation, mint and activate the next."""
    operation = authority.OperationAuthority(operation=uuid4(), direction="candidate")
    revoking = ledger.begin_revoke(home, operation)
    roles = tuple(name for entry in revoking for name in entry.roles)
    ledger.mark_closed(home, operation, authority.ClosureEvidence(roles, 0, 1))
    pending = ledger.begin_mint(home, operation, encrypt=_encrypt)
    ledger.activate(
        home,
        operation,
        authority.VerifiedGeneration(pending.number, pending.credential_digest, pending.roles),
    )


@pytest.fixture
def gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed_write_generation: Callable[[Path], Any]
) -> Path:
    """A gateway home with an active generation 0, as this process's home, with
    its human secret set (an authenticated API)."""
    home = (tmp_path / "gateway").resolve()
    home.mkdir(mode=0o700)
    seed_write_generation(home)
    monkeypatch.setattr(settings.general, "ava_home", str(home))
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _HUMAN)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    return home


def _bearer(token: str) -> str:
    return f"Bearer {token}"


# ── the generation's tokens and the gateway's acceptance ────────────────────


def test_each_generation_mints_distinct_class_tokens(gateway: Path) -> None:
    first = _tokens(gateway).api
    assert len({first.gateway, first.runner}) == 2
    assert api.acceptance(gateway) == {
        "gateway": api.token_digest(first.gateway),
        "runner": api.token_digest(first.runner),
    }
    _rotate(gateway)
    second = _tokens(gateway).api
    assert {second.gateway, second.runner}.isdisjoint({first.gateway, first.runner})
    # The rotation rewrote the ledger: the cached acceptance follows it.
    assert set(api.acceptance(gateway).values()) == {
        api.token_digest(second.gateway),
        api.token_digest(second.runner),
    }


def test_no_ledger_or_no_active_generation_accepts_no_machine_token(
    tmp_path: Path, gateway: Path
) -> None:
    bare = (tmp_path / "bare").resolve()
    bare.mkdir(mode=0o700)
    assert api.acceptance(bare) == {}
    operation = authority.OperationAuthority(operation=uuid4(), direction="candidate")
    ledger.begin_revoke(gateway, operation)  # the fence: nothing is active
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
    _rotate(gateway)
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
        _rotate(gateway)
        after = _tokens(gateway).api
        for revoked in (before.gateway, before.runner):
            assert client.get("/api/agents", headers=bearer_header(revoked)).status_code == 401
        for token in (after.gateway, after.runner, _HUMAN):
            assert client.get("/api/agents", headers=bearer_header(token)).status_code == 200


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
        _rotate(gateway)
        assert client.get("/api/bootstrap", headers=bearer_header(runner)).status_code == 401


def _login(client: TestClient, password: str) -> int:
    return client.post("/api/auth/login", json={"password": password}).status_code


def test_login_admits_the_active_runner_token_for_the_managed_browser(gateway: Path) -> None:
    tokens = _tokens(gateway).api
    with TestClient(config_app()) as client:
        assert _login(client, tokens.gateway) == 401
        response = client.post("/api/auth/login", json={"password": tokens.runner})
        assert response.status_code == 200 and response.cookies[cookie_name()]
        _rotate(gateway)
        assert _login(client, tokens.runner) == 401
        assert _login(client, _tokens(gateway).api.runner) == 200


def _cookie_authenticates(client: TestClient, cookie: str) -> tuple[int, bool]:
    """(`GET /api/agents` status, `/api/auth/check` verdict) presenting only `cookie`."""
    client.cookies.clear()
    headers = {"Cookie": f"{cookie_name()}={cookie}"}
    agents = client.get("/api/agents", headers=headers).status_code
    check = client.get("/api/auth/check", headers=headers).json()["authenticated"]
    return agents, check


def test_a_runner_minted_session_dies_with_its_generation(gateway: Path) -> None:
    """A session is only as current as the credential that minted it: the
    managed browser's cookie stops authenticating the moment the fence revokes
    the runner token that logged it in, not at the session's expiry."""
    runner = _tokens(gateway).api.runner
    with TestClient(config_app()) as client:
        cookie = client.post("/api/auth/login", json={"password": runner}).cookies[cookie_name()]
        assert _cookie_authenticates(client, cookie) == (200, True)
        _rotate(gateway)
        assert _cookie_authenticates(client, cookie) == (401, False)
        # A fresh login with the new generation's token authenticates again.
        renewed = client.post(
            "/api/auth/login", json={"password": _tokens(gateway).api.runner}
        ).cookies[cookie_name()]
        assert _cookie_authenticates(client, renewed) == (200, True)


def test_a_human_minted_session_dies_with_the_rotated_secret(
    gateway: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser logged in with the human secret survives a generation rotation
    (the human bearer is not per-generation) but not a rotation of the secret
    itself: the restarted gateway admits only sessions its current secret minted."""
    with TestClient(config_app()) as client:
        cookie = client.post("/api/auth/login", json={"password": _HUMAN}).cookies[cookie_name()]
        _rotate(gateway)
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


def test_a_machine_token_cannot_choose_the_human_secret(
    gateway: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A runner token authenticates the administrator, but it must not turn a
    generation-bound admission into a human bearer of its choosing."""
    from shared import runtime_config

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


def _webhook_request(authorization: str | None) -> Request:
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    return Request({"type": "http", "headers": headers, "client": ("10.0.0.9", 5000)})


def test_webhooks_admit_the_active_generation_off_loopback(gateway: Path) -> None:
    runner = _tokens(gateway).api.runner
    admitted = authenticate_webhook(_webhook_request(_bearer(runner)), provider="alerts")
    assert admitted == (True, "machine_token:runner")
    _rotate(gateway)
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
    _rotate(gateway)
    assert asyncio.run(_ops_status(ops_boot._ops_acceptance(), tokens.gateway)) == 401


def test_ops_posture_follows_the_api(gateway: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    assert ops_boot._ops_acceptance() is None
    assert ops_boot._ops_bind_host(None) == "127.0.0.1"
    # A remote-managed plane keeps no generations: its gateway presents the human secret.
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _HUMAN)
    monkeypatch.setattr(type(settings.data_plane), "is_remote", property(lambda _self: True))
    assert ops_boot._ops_acceptance() == frozenset({api.token_digest(_HUMAN)})


_REPO = Path(__file__).resolve().parents[3]
_PRODUCTION = (
    "agent",
    "ava",
    "ava_builtins",
    "cli",
    "gateway",
    "ops",
    "scripts",
    "services",
    "shared",
)


class _Calls(ast.NodeVisitor):
    """The innermost enclosing function of every call to `name` (`receiver.name` if given)."""

    def __init__(self, name: str, receiver: str | None) -> None:
        self.name, self.receiver = name, receiver
        self.scope: list[str] = []
        self.found: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            hit = self.receiver is None and func.id == self.name
        elif isinstance(func, ast.Attribute) and func.attr == self.name:
            owner = func.value.id if isinstance(func.value, ast.Name) else None
            hit = self.receiver is None or owner == self.receiver
        else:
            hit = False
        if hit:
            self.found.append(self.scope[-1] if self.scope else "<module>")
        self.generic_visit(node)


def _callers(name: str, *, receiver: str | None = None) -> list[str]:
    """`path::function` of every call to `name` in production code."""
    sites: set[str] = set()
    for top in _PRODUCTION:
        for path in (_REPO / top).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if name not in source:
                continue
            calls = _Calls(name, receiver)
            calls.visit(ast.parse(source))
            rel = path.relative_to(_REPO).as_posix()
            sites.update(f"{rel}::{function}" for function in calls.found)
    return sorted(sites)


def test_ops_reads_its_acceptance_once_so_only_a_root_stopped_fence_revokes() -> None:
    """`/ops` takes its acceptance at daemon boot (`_ops_acceptance`); the gateway
    re-reads it per request. "A revoked generation's token never authenticates
    again" holds for `/ops` only because no revocation runs while the daemon
    does. The ledger revokes in exactly one place, reached only through the
    release fence, whose effect `LocalTransition.fence` runs after
    `require_root_absent` (next test). A new caller here must stop the ops
    daemon first, or `/ops` must read its acceptance per request."""
    assert _callers("begin_revoke") == ["shared/cluster/authority/fence.py::revoke"]
    assert _callers("revoke") == [
        "cli/commands/data_plane/write_generation.py::fence_write_generation"
    ]
    assert _callers("fence_write_generation") == ["cli/release_transition/authority.py::fence"]
    assert _callers("fence", receiver="authority") == ["cli/release_transition/local.py::fence"]


def test_the_release_fence_revokes_only_after_root_and_its_services_are_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`require_root_absent` proves root down and every service it birthed (the
    ops daemon included) positively cleaned up; the fence revokes nothing before."""
    from cli.commands import root_driver
    from cli.release_transition import authority as release_authority
    from cli.release_transition.local import LocalTransition

    events: list[str] = []

    def root_present() -> None:
        events.append("root checked")
        raise RuntimeError("root service custody requires reconciliation")

    def revoked(_journal: object) -> None:
        events.append("revoked")

    monkeypatch.setattr(root_driver, "require_root_absent", root_present)
    monkeypatch.setattr(release_authority, "fence", revoked)
    transition = object.__new__(LocalTransition)
    transition.request = SimpleNamespace(require_configuration=lambda: None)  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="custody"):
        transition.fence(SimpleNamespace())  # type: ignore[arg-type]
    assert events == ["root checked"]
    monkeypatch.setattr(root_driver, "require_root_absent", lambda: events.append("root gone"))
    transition.fence(SimpleNamespace())  # type: ignore[arg-type]
    assert events == ["root checked", "root gone", "revoked"]


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
    monkeypatch.setattr(settings.general, "ava_home", str(home))
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
    monkeypatch.setattr("shared.bootstrap.config_source_is_local", lambda: True)
    assert bringup.api_delivery("gateway") == {api.API_TOKEN_ENV: tokens.gateway}
    assert bringup.api_delivery("runner") == {api.API_TOKEN_ENV: tokens.runner}
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    assert bringup.api_delivery("runner") == {}


def test_a_pure_runner_launch_delivers_its_capability_token(
    gateway: Path, runner_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.bootstrap.config_source_is_local", lambda: False)
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
    assert client_bearer(_HUMAN) == _HUMAN
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
