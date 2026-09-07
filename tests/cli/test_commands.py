"""Unit tests for `cli.commands` — critical paths for start / stop / status subcommands.

**Does not** e2e really start docker — all subprocess calls are monkey-patched and intercepted,
only verification of call shapes (what commands were invoked / status outputs which ✓✗).

After the PR watchdog-daemon, health checks are handled by per-capability watchdog sessions
(`ava-<cluster>-gateway-watchdog` / `ava-<cluster>-agent-runner-watchdog`,
asyncio daemon import runs each capability's healthcheck function), no longer dependent on OS
cron — so tests no longer mock crontab commands.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess as _subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from cli import commands as _cli
from cli.commands import _collect_setup_values as _real_collect_setup_values
from cli.commands import _update_uv_sync
from cli.commands._setup import SetupValues
from cli.commands.stop import _force_stop
from cli.commands.update import _poll_verdict_detail
from shared.config import settings


def _sess(service: str) -> str:
    """Expected composed session name (`ava-<service>` — no cluster segment;
    the per-home session backend scopes sessions)."""
    return f"ava-{service}"


# _noop_start_prechecks (autouse) monkey-patches _register_machine_or_die on the
# _cli module. Keep a reference to the real implementation so tests can exercise
# its actual behaviour.
_real_register_machine_or_die = _cli._register_machine_or_die
# Likewise for _wait_for_services_ready: the autouse fixture noops it on the _cli
# namespace (so the start-path tests don't stall on real probes), so the tests
# that exercise the wait itself must call the captured real implementation.
_real_wait_for_services_ready = _cli._wait_for_services_ready


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# Captured before any monkeypatch: the start-path tests stub subprocess.run to
# intercept session / docker / probe commands, but `ava start`'s migration step
# consults git (`shared.migrations._tracked_migration_paths`, Task #998) — a
# blank fake result would trip the git-tracking gate's fail-closed path and
# abort cmd_start. `git` invocations therefore reach the real binary (read-only
# rev-parse / ls-files, milliseconds).
_REAL_SUBPROCESS_RUN = _subprocess.run


def _git_aware(fake):
    """Wrap a fake subprocess.run so `git ...` calls still hit real git."""

    def _run(args, **kwargs):
        if args and args[0] == "git":
            return _REAL_SUBPROCESS_RUN(args, **kwargs)  # pyright: ignore[reportUnknownArgumentType]
        return fake(args, **kwargs)

    return _run


class _FakeSessionBackend:
    """In-memory session backend: records new/kill, answers has_session from a set.

    Stands in for the service backend (native supervisor on POSIX, winproc
    on Windows).
    """

    def __init__(self) -> None:
        self.alive: set[str] = set()
        self.created: list[str] = []
        self.killed: list[tuple[str, bool]] = []
        self.new_ok = True
        self.graceful_result: tuple[bool, str] = (True, "graceful")
        self.force_result: tuple[bool, str] = (True, "forced")
        self.signalled: list[str] = []

    def has_session(self, name: str) -> bool:
        return name in self.alive

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> bool:
        self.created.append(name)
        if self.new_ok:
            self.alive.add(name)
        return self.new_ok

    def kill_session(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str]:
        self.killed.append((name, graceful))
        if graceful:
            ok, mode = self.graceful_result
        else:
            ok, mode = self.force_result
        if ok:
            self.alive.discard(name)
        return ok, mode

    def graceful_signal(self, name: str) -> bool:
        self.signalled.append(name)
        return name in self.alive

    def list_sessions(self, prefix: str = "") -> list[str]:
        return sorted(n for n in self.alive if n.startswith(prefix))


@pytest.fixture(autouse=True)
def _fake_session_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeSessionBackend, _FakeSessionBackend]:
    """The session backends, faked in-memory for every test in this module.

    `ava start` / `ava stop` drive the service backend (native supervisor on
    POSIX, winproc on Windows) for service sessions; it must not reach the
    real supervisor in unit tests (a real launch would fork a daemon, a real
    kill could touch the dev host's sessions). Returns (service, shell).
    """
    import shared.session_backend as _sb

    service = _FakeSessionBackend()
    shell = _FakeSessionBackend()
    monkeypatch.setattr(_sb, "get_backend", lambda: service)
    monkeypatch.setattr(_sb, "get_shell_backend", lambda: shell)
    return service, shell


@pytest.fixture(autouse=True)
def _noop_start_prechecks(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_start's multi-machine setup collection + converge_host + register_self
    are all noop in this module — here we test session / docker / stop / status call shapes,
    orthogonal to setup. Setup behavior itself is left to shared/test_machine.py + the
    `setup ergonomics` section in this module.

    Default role="gateway" (full service set). To test secondary, explicitly override:
        monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
        monkeypatch.setattr(_cli, "_collect_setup_values", lambda _a: (..., []))"""

    def _fake_collect(_args: dict[str, str | None]) -> tuple[dict[str, str], list]:
        return {
            "machine_name": "test-machine",
            "machine_role": "gateway",
            "memory_remote": "git@github.com:test/AvaMemory.git",
            "gateway_url": "http://test-gateway:8000",
        }, []

    monkeypatch.setattr(_cli, "_collect_setup_values", _fake_collect)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "converge_host", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    # The per-cluster pg/redis bring-up (`_ensure_gateway_data_plane`) starts a real
    # native instance under $AVA_HOME. These tests assert session/stop/status call
    # shapes, not infra, so stub it to a noop — keeping them hermetic regardless of
    # the dev host's pg/redis.
    from cli.commands import start as _start_mod

    monkeypatch.setattr(_start_mod, "_ensure_gateway_data_plane", lambda: 0)

    # Source-integrity tests cover repair separately. A prior rollout's fake
    # installed SHA must not make these call-shape tests run a real uv sync.
    def _skip_source_integrity(_repo: Path) -> int:
        return 0

    monkeypatch.setattr(_start_mod, "_verify_source_integrity", _skip_source_integrity)
    # _roles_or_none (stop/status/converge) + machine_role (cmd_start service
    # resolution) both read settings + the machine_serve_* files; test env has
    # no file → empty/Missing. Pin both to gateway so the default path is the
    # full-service gateway box, deterministic regardless of the dev host's
    # machine_serve_* files. Agent-runner tests override machine_role explicitly.
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    # register_self goes to central DB UPSERT; test does not need real writes. cmd_start goes
    # through _register_machine_or_die which internally imports register_self, directly patch the helper to return 0.
    monkeypatch.setattr(_cli, "_register_machine_or_die", lambda _resolved, _role: 0)  # pyright: ignore[reportUnknownArgumentType]
    # secondary path will run _probe_gateway_or_die; primary does not call it, adding here
    # ensures secondary tests can also reuse the default noop.
    monkeypatch.setattr(_cli, "_probe_gateway_or_die", lambda _url: 0)  # pyright: ignore[reportUnknownArgumentType]
    # _assert_schema_current_or_die truly calls DB; tests don't need real schema query, directly patch.
    monkeypatch.setattr(_cli, "_assert_schema_current_or_die", lambda: 0)
    # The start path now polls launched services' probes before the status
    # snapshot. These tests stub subprocess, so the real probes (milvus tcp /
    # watchdog pidfile) would report not-ready and stall the wait to its timeout.
    # The wait itself is covered by its own unit tests below; noop it here.
    # Returns a ReadinessWait whose `unready` is empty (= all ready), which keeps the
    # start path's readiness gate satisfied. tests/cli/test_start_readiness_gate.py is
    # where a non-empty verdict and the exit code it produces are exercised.
    monkeypatch.setattr(
        _cli,
        "_wait_for_services_ready",
        lambda *_a, **_kw: _cli.ReadinessWait((), 0.0, sessions_gone=False),  # pyright: ignore[reportUnknownArgumentType]
    )
    # `_launch_sessions`' idempotence guard asks the service's probe as well as
    # the session (issue #1015: a live session with a dead daemon behind it must be
    # relaunched, not skipped). Same reason as the readiness wait right above: these
    # tests stub subprocess, so every real probe reports down and each "already
    # running" session would be torn down and relaunched — a call-shape assertion
    # would then be measuring the husk path instead. That path has its own tests in
    # tests/cli/test_start_husk_session.py.
    monkeypatch.setattr(_cli, "_husk_session_reason", lambda _spec: None)  # pyright: ignore[reportUnknownArgumentType]
    # _ensure_frontend_deps shells out to `npm ci` when frontend deps are stale;
    # these tests assert session call shape, not dep install, and must stay hermetic
    # regardless of whether this checkout happens to have ui/web/node_modules.
    from cli.commands import _session_lifecycle as _session_mod

    monkeypatch.setattr(_session_mod, "_ensure_frontend_deps", lambda _repo: None)  # pyright: ignore[reportUnknownArgumentType]

    # These call-shape tests use the suite's owner DB URL, not an enrolled
    # runner's bootstrap projection. Credential forwarding has its own tests
    # in test_agent_profile_launch_env.py.
    def _fixture_runner_url(_url: str) -> str:
        return "postgresql://ava_runner:test-runner@127.0.0.1:1/ava_citest"

    monkeypatch.setattr(_session_mod, "runner_db_url_projection", _fixture_runner_url)


@pytest.fixture(autouse=True)
def _hermetic_gateway_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `ava status` / `ava cluster status` HTTP calls off any real gateway.

    `cmd_status`'s gateway supplement and `cmd_cluster_status`'s roster both dial
    `gateway_api_base()`; left unstubbed they hit whatever gateway the dev box
    happens to be running (the live prod one), so the test outcome would depend
    on the environment — a live gateway on older code even crashes the supplement
    on a renamed field. Resolve it to an unreachable stub by default: tests that
    assert on the response mock httpx on top; the rest take the graceful
    'unreachable' path deterministically, matching CI where no gateway is up."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")


# ─── gateway cluster-status probe carries the bearer ───────────────────────────


def test_fetch_gateway_cluster_status_sends_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `ava status` cluster-status probe presents the cluster-secret bearer, so
    an authenticated-but-healthy gateway reads as up instead of a false 401."""
    import httpx

    from cli.commands.cluster import _fetch_gateway_cluster_status

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "s3cr3t")
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"ok": True}

    def _fake_get(url: str, *, timeout: float, headers: dict[str, str]) -> _Resp:
        captured["url"] = url
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    assert _fetch_gateway_cluster_status() == {"ok": True}
    assert captured["url"] == "http://gw:8000/api/cluster/status"
    assert captured["headers"] == {"Authorization": "Bearer s3cr3t"}


def test_fetch_gateway_cluster_status_no_bearer_when_secret_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unprovisioned (no secret): send no auth header rather than a blank bearer."""
    import httpx

    from cli.commands.cluster import _fetch_gateway_cluster_status

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {}

    def _fake_get(url: str, *, timeout: float, headers: dict[str, str]) -> _Resp:
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    _fetch_gateway_cluster_status()
    assert captured["headers"] == {}


# ─── session helpers ──────────────────────────────────────────────────────────


def test_has_session_true(
    monkeypatch, _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend]
) -> None:
    service, _shell = _fake_session_backends
    service.alive.add("ava-gateway")
    assert _cli._has_session("ava-gateway") is True


def test_has_session_false(
    monkeypatch, _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend]
) -> None:
    assert _cli._has_session("ava-missing") is False


# ─── _wait_for_services_ready ──────────────────────────────────────────────────


def _spec(service: str):
    from cli.commands._repo import ServiceSpec
    from ops.service_spec import (
        _GATEWAY,  # typed frozenset[MachineRole]; capability irrelevant to probe tests
    )

    return ServiceSpec(
        session=service,
        cmd="x",
        capabilities=_GATEWAY,
        requires_db=True,  # irrelevant to these probe tests
        curl_url="http://localhost:1/",
    )


def test_wait_returns_immediately_when_all_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """All probes already passing -> return without ever sleeping."""
    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(True, "http", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "cli.commands._probe._poll_sleep",
        lambda _s: pytest.fail("must not sleep when every probe is already ready"),  # pyright: ignore[reportUnknownArgumentType]
    )
    _real_wait_for_services_ready((_spec("gateway"), _spec("ops")), timeout_s=5.0)


def test_wait_returns_on_timeout_when_probe_stays_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe stuck at False does not hang: the deadline returns control, and it
    hands back the spec that never came up (the start path's exit-code signal)."""
    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(False, "http", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._probe._poll_sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    # timeout_s=0 -> the first failing check immediately crosses the deadline.
    wait = _real_wait_for_services_ready((_spec("gateway"),), timeout_s=0.0)
    assert [s.session for s in wait.unready] == ["gateway"]


def test_wait_returns_once_probe_flips_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Polls until a slow starter's probe flips from False to True.

    The session must be pinned alive: an unready spec whose session is *gone* will
    never bind a port, and the wait stops early on that rather than spending its
    bound — which is a different case from the slow-but-alive one under test here."""
    calls = {"n": 0}

    def _flip(_spec):
        calls["n"] += 1
        # not-ready for the first two polls
        return _cli.ServiceProbe(calls["n"] >= 3, "http", "")

    monkeypatch.setattr(_cli, "_probe_service", _flip)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._probe._poll_sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    assert _real_wait_for_services_ready((_spec("gateway"),), timeout_s=5.0).unready == ()
    assert calls["n"] == 3


def test_wait_ignores_probeless_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe-less spec (None) counts as ready and never blocks the wait."""
    monkeypatch.setattr(_cli, "_probe_service", lambda _spec: _cli.ServiceProbe(None, "n/a", ""))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "cli.commands._probe._poll_sleep",
        lambda _s: pytest.fail("n/a probe must not be treated as not-ready"),  # pyright: ignore[reportUnknownArgumentType]
    )
    _real_wait_for_services_ready((_spec("gateway-watchdog"),), timeout_s=5.0)


# ─── start (no tty required) ──────────────────────────────────────────────────


def test_cmd_start_needs_no_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_start runs without an interactive tty. The session PATH that once
    justified a tty gate is now forwarded authoritatively per session
    (shared.session_env.forward_env_dict), so start works from cron / systemd / a
    headless ssh, not only a terminal."""
    import sys as _sys

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _cli.cmd_start() == 0


# ─── schema-current guard ────────────────────────────────────────────────────


def test_cmd_start_aborts_when_schema_mismatched(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    tmp_path: Path,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """_assert_schema_current_or_die returning non-zero short-circuits cmd_start
    before register_self / session launch, so a code-vs-DB drift fails loud at start."""
    _ = tmp_path
    service, _shell = _fake_session_backends
    monkeypatch.setattr(_cli, "_assert_schema_current_or_die", lambda: 1)

    rc = _cli.cmd_start()
    assert rc == 1
    assert service.created == [], "schema-mismatch path must not launch any session"
    _ = capsys.readouterr()  # pyright: ignore[reportUnknownMemberType]


# ─── start (idempotent) ───────────────────────────────────────────────────────


def test_start_skips_existing_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """Existing sessions are skipped, no duplicate launch."""
    _ = tmp_path
    service, _shell = _fake_session_backends
    service.alive = {
        _sess(spec.session) for spec in _cli._services_for_roles(frozenset({"gateway"}))
    }

    def fake_run(args, **_kwargs):
        return _FakeResult(returncode=0)

    monkeypatch.setattr(_cli.subprocess, "run", _git_aware(fake_run))  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli.cmd_start()
    assert rc == 0
    assert service.created == []


def test_start_creates_missing_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """Session does not exist → launch, once for each service in the gateway set.

    role=gateway excludes ava-ops (gateway is the gateway itself, does not run an ops server against itself),
    so launch count = len(_services_for_role("gateway")) not len(build_services()).
    """
    _ = tmp_path
    service, _shell = _fake_session_backends

    def fake_run(args, **_kwargs):
        return _FakeResult(returncode=0)

    monkeypatch.setattr(_cli.subprocess, "run", _git_aware(fake_run))  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli.cmd_start()
    assert rc == 0
    expected = _cli._services_for_roles(frozenset({"gateway"}))
    assert len(service.created) == len(expected)
    assert _sess("ops") not in service.created


def test_start_includes_watchdog_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """The gateway watchdog is a build_services() entry; a gateway host's
    `ava start` (the autouse default role) starts gateway-watchdog and NOT the
    agent-runner one."""
    _ = tmp_path
    service, _shell = _fake_session_backends

    def fake_run(args, **_kw):
        return _FakeResult(returncode=0)

    monkeypatch.setattr(_cli.subprocess, "run", _git_aware(fake_run))  # pyright: ignore[reportUnknownArgumentType]
    _cli.cmd_start()
    assert _sess("gateway-watchdog") in service.created
    assert _sess("agent-runner-watchdog") not in service.created  # gateway-only host


# ─── start (secondary node only starts ops/agent-host/agent-runner-watchdog + skips local infra) ─


def test_start_agent_runner_skips_local_infra(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """Secondary node does not start local pg/redis (uses the central node's DB/Redis/Milvus)."""
    _ = tmp_path

    def _secondary_collect(_args: dict[str, str | None]) -> tuple[dict[str, str], list]:
        return {
            "machine_name": "wsl",
            "machine_role": "agent-runner",
            "memory_remote": "git@github.com:test/AvaMemory.git",
            "gateway_url": "https://gateway.test.example/",
        }, []

    monkeypatch.setattr(_cli, "_collect_setup_values", _secondary_collect)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"agent-runner"}))

    # the data-plane bring-up must NOT be called for an agent-runner-only host.
    from cli.commands import start as _start_mod

    infra_calls: list[int] = []
    monkeypatch.setattr(
        _start_mod, "_ensure_gateway_data_plane", lambda: infra_calls.append(1) or 0
    )

    def fake_run(args, **_kw):
        return _FakeResult(returncode=0)

    monkeypatch.setattr(_cli.subprocess, "run", _git_aware(fake_run))  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_start()
    assert rc == 0
    assert infra_calls == [], f"secondary must not start local infra, actually called {infra_calls}"


def test_start_agent_runner_starts_only_minimal_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """secondary only starts ops, agent-host and agent-runner services."""
    _ = tmp_path
    monkeypatch.setattr("shared.config.settings.services.browser_enabled", False)  # env-independent
    # Pin computer-mcp's platform gate "available" (env-independent roster).
    monkeypatch.setattr("ops.spec._computer_mcp_gate_reason", lambda: None)
    # The process-mode startup shape (hosted is the default since 2026-09).

    def _secondary_collect(_args: dict[str, str | None]) -> tuple[dict[str, str], list]:
        return {
            "machine_name": "wsl",
            "machine_role": "agent-runner",
            "memory_remote": "git@github.com:test/AvaMemory.git",
            "gateway_url": "https://gateway.test.example/",
        }, []

    monkeypatch.setattr(_cli, "_collect_setup_values", _secondary_collect)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"agent-runner"}))

    service, _shell = _fake_session_backends

    def fake_run(args, **_kw):
        return _FakeResult(returncode=0)

    monkeypatch.setattr(_cli.subprocess, "run", _git_aware(fake_run))  # pyright: ignore[reportUnknownArgumentType]
    _cli.cmd_start()
    assert set(service.created) == {
        _sess("ops"),
        _sess("agent-host"),
        _sess("page-server"),
        _sess("agent-runner-watchdog"),
        _sess("computer-mcp"),
        _sess("mcp-daemon"),
        _sess("otel-collector"),
    }, (
        f"secondary should start these sessions (no local gateway), actually started {sorted(service.created)}"
    )


def test_services_for_role_gateway_excludes_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """role=gateway → build_services() minus agent-runner-only sessions.

    ops: gateway is the gateway itself, does not run ops server against itself.
    browser: agent-runner-only + opt-in (default off here) — dropped both ways.
    restarter / agent-runner-watchdog: agent-runner-only now (gateway has no
    agents to respawn; the gateway runs gateway-watchdog instead).
    """
    # The exact roster includes the designated gateway's collector; pin its
    # marker gate open so this capability-partition assertion is host-independent.
    monkeypatch.setattr("ops.spec._otel_collector_gate_reason", lambda: None)
    sessions = {s.session for s in _cli._services_for_roles(frozenset({"gateway"}))}
    all_sessions = {s.session for s in _cli.build_services()}
    assert sessions == all_sessions - {
        "ops",
        "page-server",
        "browser",
        "browser-mcp",
        "mcp-daemon",
        "computer-mcp",
        "agent-runner-watchdog",
        # agent-host: agent-runner-only — never on a gateway-only host, in
        # either runner mode.
        "agent-host",
        "pitr-uploader",
        "pitr-base-candidate",
        # milvus is gated out under the numpy memory-search backend (the
        # default) — see ops.spec._gate_reason.
        "milvus",
    }
    assert "ops" not in sessions
    assert "browser" not in sessions
    assert "browser-mcp" not in sessions  # agent-runner-only, like browser
    assert "restarter" not in sessions
    assert "agent-runner-watchdog" not in sessions
    assert "gateway-watchdog" in sessions  # the gateway's own watchdog


def test_services_for_role_agent_runner_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    """role=agent-runner → ops (inbound server), agent-host and agent-runner-watchdog.
    No local gateway, no gateway-watchdog."""
    monkeypatch.setattr("shared.config.settings.services.browser_enabled", False)  # env-independent
    # computer-mcp's gate is the platform's permissions-helper capability, not a
    # setting — pin it "available" so the roster is env-independent (CI hosts
    # lack the helper and would otherwise drop the service).
    monkeypatch.setattr("ops.spec._computer_mcp_gate_reason", lambda: None)
    # This synthetic runner roster includes its relay collector; pin the gate
    # open so the actual host's gateway marker cannot perturb the assertion.
    monkeypatch.setattr("ops.spec._otel_collector_gate_reason", lambda: None)
    # Pin the process partition: hosted (the default since 2026-09) swaps
    # restarter for agent-host on this roster.
    sessions = {s.session for s in _cli._services_for_roles(frozenset({"agent-runner"}))}
    assert sessions == {
        "ops",
        "agent-host",
        "page-server",
        "agent-runner-watchdog",
        "computer-mcp",
        "mcp-daemon",
        "otel-collector",
    }
    assert "gateway-watchdog" not in sessions


def test_services_for_roles_single_box_unions_both(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-box gateway,agent-runner host runs the UNION — every gateway
    daemon PLUS ops (so its own gateway can dial it over localhost for spawn),
    both capability watchdogs, and one agent host."""
    monkeypatch.setattr("shared.config.settings.services.browser_enabled", False)
    # computer-mcp's gate is the platform's permissions-helper capability, not a
    # setting — pin it "available" so the union is env-independent.
    monkeypatch.setattr("ops.spec._computer_mcp_gate_reason", lambda: None)
    # The exact union includes the designated gateway's collector; pin its
    # marker gate open so this capability-partition assertion is host-independent.
    monkeypatch.setattr("ops.spec._otel_collector_gate_reason", lambda: None)
    # Pin the runner mode BEFORE computing the roster: hosted is the default
    # since 2026-09, so this asserts the default shape — agent-host in,
    # restarter out.
    sessions = {s.session for s in _cli._services_for_roles(frozenset({"gateway", "agent-runner"}))}
    all_sessions = {s.session for s in _cli.build_services()}
    # union = everything that is not gated out; browser + browser-mcp are off
    # above (build_services still lists them, services_for_capabilities drops
    # them), computer-mcp is pinned available; ops IS present.
    assert sessions == all_sessions - {
        "browser",
        "browser-mcp",
        "pitr-uploader",
        "pitr-base-candidate",
        # milvus is gated out under the numpy memory-search backend (the
        # default) — see ops.spec._gate_reason.
        "milvus",
    }
    assert "ops" in sessions  # the load-bearing addition vs gateway-only
    assert "gateway" in sessions
    assert "agent-host" in sessions  # the hosted runner, by default
    assert "restarter" not in sessions  # process supervision retired in hosted
    assert "gateway-watchdog" in sessions
    assert "agent-runner-watchdog" in sessions


# ─── setup ergonomics (args priority + missing fail-loud, no TTY) ────────────────────


def test_start_missing_capability_reports_serve_flags_only(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """serve-capability is the entry to the role-aware filter; when both are missing (host serves nothing)
    other fields cannot be judged for relevance, so the error lists only the two --serve-* flags
    rather than listing all fields."""

    monkeypatch.setattr(settings.general, "machine_name", "")
    monkeypatch.setattr(settings.general, "machine_serve_gateway", None)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", None)
    monkeypatch.setattr(settings.general, "machine_serve_observability_station", None)
    monkeypatch.setattr(settings.general, "memory_remote", "")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    from shared import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_cli, "_collect_setup_values", _real_collect_setup_values)

    rc = _cli.cmd_start()
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "missing required" in err
    assert "--serve-gateway" in err
    assert "--serve-agent-runner" in err
    # When capability is not resolved, the example should give both gateway + agent-runner commands
    assert "ava start --machine-name <name> --serve-gateway " in err
    assert "ava start --machine-name <name> --serve-agent-runner " in err


def test_start_missing_agent_runner_fields_reports_agent_runner_flags(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """capability is agent-runner, other fields missing → error lists agent-runner needed flags (--gateway-url)."""

    monkeypatch.setattr(settings.general, "machine_name", "")
    monkeypatch.setattr(settings.general, "machine_serve_gateway", None)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", True)
    monkeypatch.setattr(settings.general, "memory_remote", "")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    from shared import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_cli, "_collect_setup_values", _real_collect_setup_values)

    rc = _cli.cmd_start()
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "--machine-name" in err
    assert "--gateway-url" in err


def test_start_missing_gateway_fields_reports_gateway_flags(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """capability=gateway, other fields missing → error lists gateway needed flags (--gateway-url)."""

    monkeypatch.setattr(settings.general, "machine_name", "")
    monkeypatch.setattr(settings.general, "machine_serve_gateway", True)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", None)
    monkeypatch.setattr(settings.general, "machine_serve_observability_station", None)
    monkeypatch.setattr(settings.general, "memory_remote", "")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    from shared import paths

    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path / "unconfigured")
    monkeypatch.setattr(_cli, "_collect_setup_values", _real_collect_setup_values)

    rc = _cli.cmd_start()
    assert rc == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "--machine-name" in err
    assert "--gateway-url" in err


def test_start_arg_writes_to_file_for_persistence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unit_home
) -> None:
    """Passing arg causes the cli to write the value to the $AVA_HOME/<field> file,
    so next start doesn't need to pass it again."""

    # all env empty, files also do not exist
    monkeypatch.setattr(settings.general, "machine_name", "")
    monkeypatch.setattr(settings.general, "machine_serve_gateway", None)
    monkeypatch.setattr(settings.general, "machine_serve_agent_runner", None)
    monkeypatch.setattr(settings.general, "machine_serve_observability_station", None)
    monkeypatch.setattr(settings.general, "memory_remote", "")
    monkeypatch.setattr(settings.gateway, "gateway_url", "")

    resolved, missing = _real_collect_setup_values(
        {
            "machine_name": "test-host",
            "machine_serve_gateway": True,
            "machine_serve_agent_runner": None,
            "machine_serve_observability_station": None,
            "machine_description": None,
            "memory_remote": "git@github.com:test/AvaMemory.git",
            "gateway_url": "https://ava.example.com",
        }
    )
    assert missing == []
    assert resolved == {
        "machine_name": "test-host",
        "machine_role": "gateway",
        "memory_remote": "git@github.com:test/AvaMemory.git",
        "gateway_url": "https://ava.example.com",
    }
    # serve_gateway capability file + 3 string fields written
    assert (tmp_path / "machine_serve_gateway").read_text() == "true"
    assert not (tmp_path / "machine_serve_agent_runner").exists()  # arg=None, not written
    assert (tmp_path / "machine_name").read_text() == "test-host"
    assert (tmp_path / "memory_remote").read_text() == "git@github.com:test/AvaMemory.git"
    assert (tmp_path / "gateway_url").read_text() == "https://ava.example.com"


# ─── restart ─────────────────────────────────────────────────────────────────


def test_cmd_restart_succeeds_non_interactively(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_restart succeeds in a non-interactive context (the detached-updater path) —
    like cmd_start, it needs no tty."""
    import sys as _sys

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cli, "_do_stop", MagicMock(return_value=0))
    rc = _cli.cmd_restart()
    assert rc == 0


def test_cmd_restart_calls_stop_then_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_restart invokes stop (require_confirmation=False) then _cmd_start_body in order."""
    order: list[str] = []

    def fake_do_stop(
        _repo, *, graceful, require_confirmation, keep_infra=False, force=False
    ) -> int:
        assert require_confirmation is False, "cmd_restart must skip stdin confirmation"
        assert force is False
        assert keep_infra is True, (
            "an internal restart must keep the shared pg/redis up — stopping the "
            "data plane kills the orchestrator's DB polling mid-rollout"
        )
        order.append("stop")
        return 0

    def fake_cmd_start_body(**kwargs: object) -> int:
        assert kwargs["updater_telemetry"] is True
        order.append("start")
        return 0

    monkeypatch.setattr(_cli, "_do_stop", fake_do_stop)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_cmd_start_body", fake_cmd_start_body)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_restart()
    assert order == ["stop", "start"]
    assert rc == 0


def test_cmd_restart_records_its_full_wall_time_as_an_updater_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restart marker's self-contained duration must win over the Windows
    ladder's first nested marker, which otherwise records only the preflight lead-in."""
    from contextlib import contextmanager

    from cli.commands import stop as stop_mod

    seen: list[str] = []

    @contextmanager
    def _stage(name: str):
        seen.append(name)
        yield

    monkeypatch.setattr(stop_mod, "updater_stage", _stage)
    monkeypatch.setattr(_cli, "_preflight_probes", lambda: 0)
    monkeypatch.setattr(_cli, "_do_stop", lambda *_args, **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_cmd_start_body", lambda **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _cli.cmd_restart() == 0
    assert seen[0] == "restart"


def test_cmd_restart_short_circuits_on_stop_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If stop returns non-zero, cmd_restart propagates without calling start."""
    start_called: list[bool] = []

    monkeypatch.setattr(_cli, "_do_stop", lambda *_a, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_cmd_start_body", lambda **_kw: start_called.append(True) or 0)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_restart()
    assert rc == 1
    assert start_called == [], "start must not run when stop fails"


def test_cmd_restart_aborts_when_preflight_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """When preflight probes fail, cmd_restart aborts without stopping — and says so
    with its OWN exit code, since "nothing was stopped, host still serving" is what
    the detached updater must not run `ava start` over."""
    from shared.exit_codes import RESTART_DECLINED_EXIT_CODE

    stopped: list[bool] = []
    start_called: list[bool] = []

    monkeypatch.setattr(_cli, "_preflight_probes", lambda: 1)  # simulate failure
    monkeypatch.setattr(_cli, "_do_stop", lambda *_a, **_kw: stopped.append(True) or 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_cmd_start_body", lambda **_kw: start_called.append(True) or 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_release_self_heal_pause", lambda: None)

    rc = _cli.cmd_restart()
    assert rc == RESTART_DECLINED_EXIT_CODE, "preflight failure must propagate non-zero"
    assert stopped == [], "must not stop services when preflight fails"
    assert start_called == [], "must not start when preflight fails"


# ─── stop (stdin confirmation) ────────────────────────────────────────────────────────


def test_stop_aborts_on_no(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdin input not y → abort, no kill / down commands called."""
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(args, **_kwargs):
        raise AssertionError(f"subprocess.run should not be called: {args}")

    monkeypatch.setattr(_cli.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_stop(force=True)
    assert rc == 0


def test_stop_proceeds_on_yes(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """stdin y → call the session kill (both backends) + stop shared pg/redis."""
    gateway_sess = _sess("gateway")
    service, _shell = _fake_session_backends
    service.alive.add(gateway_sess)
    monkeypatch.setattr(_cli, "_has_session", lambda s: s == gateway_sess)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")  # pyright: ignore[reportUnknownArgumentType]

    # This cluster's own pg/redis teardown lives behind `stop_cluster_instance`
    # (pg_ctl stop + redis shutdown for its private instance) — track the call here.
    infra_stops: list[int] = []
    monkeypatch.setattr(
        "cli.commands._cluster_instance.stop_cluster_instance",
        lambda: infra_stops.append(1) or 0,
    )

    rc = _cli.cmd_stop(force=True)
    assert rc == 0
    # the force path kills the session on the service backend
    assert (gateway_sess, False) in service.killed
    assert len(infra_stops) == 1, "stop must stop this cluster's pg/redis once"


def test_stop_revokes_serving_before_stopping_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A deliberate stop removes recovery authority before daemons unwind."""
    from cli.commands import stop as stop_mod
    from shared import start_serving

    path = tmp_path / "start-serving.json"
    monkeypatch.setattr(start_serving, "state_path", lambda: path)
    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation) is True
    observed: list[bool] = []

    def _compute_stop_scope(
        *, preserve_sessions: frozenset[str], keep_browser: bool, keep_infra: bool
    ) -> tuple[list[str], bool, bool]:
        return [], False, True

    def _print_stop_plan(
        service_sessions: list[str],
        *,
        reap_agents: bool,
        keep_browser: bool,
        runner_only: bool,
        keep_infra: bool,
    ) -> None:
        return None

    def _stop_data_plane(*, skip_infra: bool, runner_only: bool) -> None:
        return None

    def _reap_orphan_step(
        repo: Path,
        *,
        keep_browser: bool,
        keep_infra: bool,
        preserve_sessions: frozenset[str],
        keep_gate: bool,
    ) -> None:
        return None

    def _stop_sessions(sessions: list[str]) -> None:
        observed.append(start_serving.is_serving())

    monkeypatch.setattr(stop_mod, "_compute_stop_scope", _compute_stop_scope)
    monkeypatch.setattr(stop_mod, "_print_stop_plan", _print_stop_plan)
    monkeypatch.setattr(stop_mod, "_stop_data_plane", _stop_data_plane)
    monkeypatch.setattr(stop_mod, "_reap_orphan_step", _reap_orphan_step)
    monkeypatch.setattr(stop_mod, "_stop_sessions", _stop_sessions)

    assert stop_mod._force_stop(tmp_path, require_confirmation=False) == 0
    assert observed == [False]


def test_do_stop_keep_infra_skips_infra_teardown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """`_do_stop(keep_infra=True)` skips stopping shared pg/redis — cmd_update orchestrator
    uses this to keep the DB alive during graceful stop, otherwise the next step
    apply_pending_migrations would immediately get connect refused (verified in
    2026-05-19 prod incident).
    """
    gateway_sess = _sess("gateway")
    service, _shell = _fake_session_backends
    service.alive.add(gateway_sess)
    monkeypatch.setattr(_cli, "_has_session", lambda s: s == gateway_sess)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))

    infra_stops: list[int] = []
    monkeypatch.setattr(
        "cli.commands._cluster_instance.stop_cluster_instance",
        lambda: infra_stops.append(1) or 0,
    )

    rc = _force_stop(tmp_path, require_confirmation=False, keep_infra=True)
    assert rc == 0
    # The explicit force path ends the selected services and retains infra.
    assert service.signalled == []
    assert (gateway_sess, False) in service.killed
    # this cluster's pg/redis **not** stopped (keep_infra keeps DB alive before migrate)
    assert infra_stops == []


def test_do_stop_keeps_browser_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An in-place stop / update leaves the headed browser session running so the
    login Chrome is not bounced (keep_browser defaults True)."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway", "agent-runner"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    killed: list[str] = []
    monkeypatch.setattr(_cli, "_kill_session", lambda s, **_kw: killed.append(s) or True)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr("cli.commands._cluster_instance.stop_cluster_instance", lambda: 0)

    reaps: list[int] = []
    monkeypatch.setattr(_cli, "_reap_cluster_chrome", lambda: reaps.append(1))

    rc = _force_stop(tmp_path, require_confirmation=False)
    assert rc == 0
    assert _sess("browser") not in killed, "browser session must be preserved by default"
    assert _sess("gateway") in killed, "non-browser services are still stopped"
    assert reaps == [], "a stop that preserves the browser must not sweep its Chrome"


def test_do_stop_stop_browser_kills_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """keep_browser=False (full teardown / `ava cluster destroy`) takes the browser
    session down too, AND sweeps a Chrome that left that session on a SingletonLock
    handoff — a destroyed cluster must not leave an orphan headed Chrome holding
    the cluster's CDP port."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway", "agent-runner"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    killed: list[str] = []
    monkeypatch.setattr(_cli, "_kill_session", lambda s, **_kw: killed.append(s) or True)  # pyright: ignore[reportUnknownArgumentType]

    monkeypatch.setattr("cli.commands._cluster_instance.stop_cluster_instance", lambda: 0)

    order: list[str] = []
    monkeypatch.setattr(_cli, "_kill_session", lambda s, **_kw: (killed.append(s), order.append(s)))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_reap_cluster_chrome", lambda: order.append("reap"))

    rc = _force_stop(tmp_path, require_confirmation=False, keep_browser=False)
    assert rc == 0
    assert _sess("browser") in killed, "keep_browser=False must stop the browser session"
    assert "reap" in order, "keep_browser=False must also sweep the cluster's Chrome"
    # Ordering matters: the watchdog is already dead when the sweep runs, so
    # nothing relaunches Chrome onto the port the sweep just cleared.
    assert order.index("reap") == len(order) - 1, "the sweep runs after every session kill"


def test_reap_cluster_chrome_reports_pids_and_survives_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """The CLI seam: report what was reaped, stay silent when there was nothing,
    and never let a sweep failure fail the teardown around it."""
    from cli.commands import stop as _stop_mod

    monkeypatch.setattr("services.browser.orphan.reap_cluster_chrome", lambda: [4242])
    _stop_mod._reap_cluster_chrome()
    assert "4242" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]

    monkeypatch.setattr("services.browser.orphan.reap_cluster_chrome", list)
    _stop_mod._reap_cluster_chrome()
    assert capsys.readouterr().out == "", "nothing to reap prints nothing"  # pyright: ignore[reportUnknownMemberType]

    def _boom() -> list[int]:
        raise RuntimeError("process table unavailable")

    monkeypatch.setattr("services.browser.orphan.reap_cluster_chrome", _boom)
    _stop_mod._reap_cluster_chrome()  # must not raise
    assert "could not sweep" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_cmd_stop_stop_browser_flag_threads_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`cmd_stop(stop_browser=...)` maps to `_do_stop(keep_browser=not stop_browser)`."""
    from cli.commands import stop as _stop_mod

    seen: dict[str, object] = {}

    def fake_do_stop(_repo, *, keep_browser=True, **_kw) -> int:
        seen["keep_browser"] = keep_browser
        return 0

    monkeypatch.setattr(_stop_mod, "_do_stop", fake_do_stop)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_mod, "_repo_root", lambda: tmp_path)

    _stop_mod.cmd_stop(require_confirmation=False)
    assert seen["keep_browser"] is False, "default cmd_stop closes the browser"
    _stop_mod.cmd_stop(require_confirmation=False, stop_browser=True)
    assert seen["keep_browser"] is False, "--stop-browser takes the browser down"


# ─── graceful stop / update (PR ava-update) ─────────────────────────────────


def test_graceful_kill_session_noop_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """session does not exist → (True, 'noop'), idempotent."""
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    ok, mode = _cli._graceful_kill_session("ava-missing", timeout_s=0.5)
    assert ok
    assert mode == "noop"


def test_graceful_kill_session_forwards_to_the_backend(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """The cli seam forwards a graceful kill to the service backend and reports
    its mode."""
    service, _shell = _fake_session_backends
    # noop precheck (cli layer) sees the session alive → proceed to the graceful kill
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]

    ok, mode = _cli._graceful_kill_session("ava-gateway", timeout_s=10.0)
    assert ok
    assert mode == "graceful"
    assert ("ava-gateway", True) in service.killed


def test_graceful_kill_session_forced_fallback_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """The backend's graceful-then-force escalation is surfaced as (True, 'forced')
    — the cli seam passes the mode through, it does not decide it."""
    service, _shell = _fake_session_backends
    service.graceful_result = (True, "forced")
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]

    ok, mode = _cli._graceful_kill_session("ava-gateway", timeout_s=0.01)
    assert ok
    assert mode == "forced"
    assert ("ava-gateway", True) in service.killed


# ─── _stop_sessions: the printed marker must carry the confirmation ──────


def test_force_stop_ends_controllers_before_dependents(
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    from cli.commands.stop import _stop_sessions

    service, _ = _fake_session_backends
    targets = ["ava-gateway", "ava-agent-host", "ava-gateway-watchdog", "ava-ops"]
    service.alive.update(targets)
    _stop_sessions(targets)
    assert service.killed[0] == ("ava-gateway-watchdog", False)
    assert {name for name, graceful in service.killed if not graceful} == set(targets)
    assert service.signalled == []


def test_stop_sessions_force_path_reports_failure(monkeypatch, capsys) -> None:
    """The explicit force path (`ava stop --force`) also stops printing ✓ for a kill that was
    not confirmed: `_kill_session` answering False is ✗."""
    from cli.commands.stop import _stop_sessions

    monkeypatch.setattr(_cli, "_kill_session", lambda _s, **_kw: False)  # pyright: ignore[reportUnknownMemberType]

    with pytest.raises(RuntimeError, match="force stop"):
        _stop_sessions(["ava-gateway"])

    assert "✗ ava-gateway" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_stop_scope_includes_only_service_backend_sessions(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """The stop plan covers every service session alive on the SERVICE backend
    and nothing else — a same-named session on the orchestration backend (the
    pre-switch leftovers, gone since the migration) is not the stop's
    business, and must never be killed by it."""
    from cli.commands.stop import _compute_stop_scope

    service, shell = _fake_session_backends
    service.alive.add(_sess("gateway"))
    shell.alive.add(_sess("labeler"))

    sessions, _runner_only, _skip = _compute_stop_scope(
        preserve_sessions=frozenset(), keep_browser=True, keep_infra=False
    )
    assert _sess("gateway") in sessions
    assert _sess("labeler") not in sessions, (
        "orchestration-side sessions are never in the stop plan"
    )


def test_stop_scope_is_empty_when_no_session_on_either_backend(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """No session on either backend → an empty stop plan (nothing to kill)."""
    from cli.commands.stop import _compute_stop_scope

    sessions, _runner_only, _skip = _compute_stop_scope(
        preserve_sessions=frozenset(), keep_browser=True, keep_infra=False
    )
    assert sessions == []


# ─── status ───────────────────────────────────────────────────────────────────


def test_status_runs_without_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """status can output normally even when all command mocks return non-0 (empty cluster) (no raise)."""
    _ = capsys
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(_cli.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("gateway") in out
    assert _sess("frontend") in out


def test_status_gateway_excludes_ops(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(_cli.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("gateway") in out
    assert _sess("ops") not in out


def test_status_agent_runner_shows_ops(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("ops") in out
    assert _sess("gateway") not in out


def test_status_shows_browser_skip_reason(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Issue #1111: an enabled-but-incapable ava-browser is shown WITH its reason
    rather than silently dropped, so `ava status` (the first diagnostic command)
    is not blind to the broken service."""
    from cli.commands import _repo

    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_repo.settings.services, "browser_enabled", True)
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: "no display (headless)")
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(_args, **_kwargs):
        return _FakeResult(returncode=0, stdout="")

    monkeypatch.setattr(_cli.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("browser") in out
    assert "skipped: no display" in out


def test_status_shows_the_gate_entry_row(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The fleet UI entry port is on the status screen. On 2026-08-01 a converge
    killed the gate and failed to reinstall it: every service row stayed green
    (they probe the app slot BEHIND the gate) while :3000 answered nothing."""
    import cli.commands._converge_gate as cg

    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        cg,
        "probe_gate",
        lambda *_a: cg.GateStatus(3000, 3001, False, True, "launchd job com.ava.gate.x"),  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "gate (fleet UI entry):" in out
    assert "entry :3000 not answering" in out


def test_status_shows_the_end_to_end_redis_bridge_row(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """The host-level relay must not disappear behind healthy service rows."""
    import cli.commands.status as status_mod

    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        status_mod,
        "print_redis_bridge_status",
        lambda: sys.stdout.write("  ✗ 10.64.0.7:6380 Redis PING: connection refused\n"),
    )

    assert _cli.cmd_status() == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "redis bridge (private-network ingress):" in out
    assert "Redis PING: connection refused" in out


def test_status_runner_only_has_no_gate_section(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A pure agent-runner owns no entry port — same rule as the pg/redis section."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "gate (fleet UI entry):" not in out
    assert "redis bridge (private-network ingress):" not in out


def test_start_prints_browser_skip_reason(monkeypatch, capsys, tmp_path) -> None:
    """`ava start` (_launch_sessions) prints the gated-out browser + reason on
    the console — the start-time analogue of the `ava status` row, so the roster
    never silently shrinks. _has_session->True keeps it from launching anything."""
    from cli.commands import _repo

    monkeypatch.setattr(_repo.settings.services, "browser_enabled", True)  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr("ops.spec.browser_incapability", lambda: "no display (headless)")  # pyright: ignore[reportUnknownMemberType]
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownMemberType]

    _cli._launch_sessions(frozenset({"agent-runner"}), set(), tmp_path)  # pyright: ignore[reportUnknownArgumentType]
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert _sess("browser") in out
    assert "skipped: no display" in out


def test_service_row_shows_live_session_with_skip_reason(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """A still-running session that is now gated reads as `✓ ... -- skipped: <reason>`
    — marks reflect real liveness, the suffix the gate — surfacing the mismatch the
    _print_service_row comment advertises."""
    monkeypatch.setattr(_cli, "_has_session", lambda _s: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: True)  # pyright: ignore[reportUnknownArgumentType]

    spec = next(s for s in _cli.build_services() if s.session == "browser")
    _cli._print_service_row(spec, 16, "no display (headless)")
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "✓" in out  # liveness mark still shown
    assert "skipped: no display" in out


# ─── prod source drift detection (any installed host) ────────────────────────


def _init_prod_source(source: Path, *, branch: str = "main") -> None:
    """Create a real git repo at `source` with one commit on `main`; optionally
    leave it checked out on a different branch (the dev-on-prod-tree mistake)."""
    import subprocess

    source.mkdir(parents=True)

    def run(*args: str) -> None:
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)  # noqa: S603

    run("init", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (source / "f").write_text("x")
    run("add", ".")
    run("commit", "-m", "init")
    if branch != "main":
        run("checkout", "-b", branch)


def test_detect_prod_source_drift_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No source repo → None (nothing to check)."""
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert _cli._detect_prod_source_drift() is None


def test_detect_prod_source_drift_on_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Prod source on `main` → None (no drift)."""
    _init_prod_source(tmp_path / "source")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert _cli._detect_prod_source_drift() is None


def test_detect_prod_source_drift_feature_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Prod source on a feature branch → returns the branch (the 2026-06-01
    incident: an agent developing in the prod tree instead of a worktree)."""
    _init_prod_source(tmp_path / "source", branch="ava-7/fix")
    monkeypatch.setattr("shared.cluster_drift._prod_source_dir", lambda: tmp_path / "source")
    assert _cli._detect_prod_source_drift() == "ava-7/fix"


def test_cmd_status_warns_on_prod_source_drift(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """cmd_status surfaces the drift warning when the prod source is off main
    (runs on any installed host, here agent-runner)."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.status._detect_prod_source_drift", lambda: "ava-7/fix")
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "prod source" in out
    assert "ava-7/fix" in out


# ─── cluster pin (cluster_target_sha) status ─────────────────────────────────


def test_cluster_pin_status_no_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pin set yet → None (no line to show)."""
    monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", lambda: None)
    assert _cli._cluster_pin_status() is None


def test_cluster_pin_status_returns_pin_and_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin set → (target_sha, this_host_head)."""
    monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", lambda: "abc1234")
    monkeypatch.setattr("cli.commands._probe._prod_source_head_sha", lambda: "abc1234")
    assert _cli._cluster_pin_status() == ("abc1234", "abc1234")


def test_cluster_pin_status_db_unreachable_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down central DB → None (ava status must still run; the pin is diagnostic)."""
    import psycopg

    def _boom() -> str | None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr("shared.cluster_pin.get_cluster_target_sha", _boom)
    assert _cli._cluster_pin_status() is None


def test_cmd_status_shows_cluster_pin_aligned(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """cmd_status prints the cluster-pin line; HEAD == pin → aligned."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.status._detect_prod_source_drift", lambda: None)
    monkeypatch.setattr("cli.commands.status._cluster_pin_status", lambda: ("abc1234", "abc1234"))
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "cluster pin: abc1234" in out
    assert "aligned" in out


@pytest.mark.parametrize(
    ("relation", "expected"),
    [
        ("behind", "behind pin"),
        ("ahead", "ahead of pin"),
        ("diverged", "diverged from pin"),
        ("unknown", "off pin"),
    ],
)
def test_cmd_status_cluster_pin_drift_wording(
    monkeypatch: pytest.MonkeyPatch, capsys, relation, expected
) -> None:
    """HEAD != pin: the mark reflects the git relation to the pin, not a flat 'behind'.
    'ahead' is the stray-`git pull` case the old flat wording mislabelled as behind."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.status._detect_prod_source_drift", lambda: None)
    monkeypatch.setattr("cli.commands.status._cluster_pin_status", lambda: ("aaaaaaa", "bbbbbbb"))
    monkeypatch.setattr("cli.commands.status.prod_source_pin_relation", lambda _p, _h: relation)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    assert expected in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_cmd_status_cluster_pin_head_unreadable(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """HEAD can't be read (head is None) → 'HEAD unreadable', no relation computed."""
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands.status._detect_prod_source_drift", lambda: None)
    monkeypatch.setattr("cli.commands.status._cluster_pin_status", lambda: ("aaaaaaa", None))
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    assert "HEAD unreadable" in out


# ─── ava cluster status roster pin column ────────────────────────────────────


def test_pin_cell_on_pin() -> None:
    from cli.commands.cluster import _pin_cell

    assert _pin_cell(on_pin=True, head_sha="abc1234def") == "✓ abc1234"


def test_pin_cell_off_pin() -> None:
    from cli.commands.cluster import _pin_cell

    assert _pin_cell(on_pin=False, head_sha="abc1234def") == "✗ abc1234"


def test_pin_cell_unknown() -> None:
    from cli.commands.cluster import _pin_cell

    assert _pin_cell(None, None) == "? —"


def test_code_cell_matches_checkout() -> None:
    """running_sha == head_sha → the short SHA with no drift marker."""
    from cli.commands.cluster import _code_cell

    assert _code_cell(running_sha="abc1234def", head_sha="abc1234def") == "abc1234"


def test_code_cell_drift_marks_stale_process() -> None:
    """running_sha != head_sha → ⚠ + running short SHA (process running stale code
    vs its checkout, even when pin reads ✓)."""
    from cli.commands.cluster import _code_cell

    assert _code_cell(running_sha="999888777", head_sha="abc1234def") == "⚠ 9998887"


def test_code_cell_unknown_running_sha() -> None:
    """No running_sha recorded → em dash."""
    from cli.commands.cluster import _code_cell

    assert _code_cell(running_sha=None, head_sha="abc1234def") == "—"


def test_status_cell_identity_mismatch_outranks_online() -> None:
    """identity_mismatch renders a loud MISMATCH even when online is True — a
    wrong-identity responder is never shown as a plain online host."""
    from datetime import UTC, datetime

    from cli.commands.cluster import _status_cell

    stopped = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)
    assert _status_cell(online=True, identity_mismatch=True, stopped_at=None) == "MISMATCH"
    assert _status_cell(online=True, identity_mismatch=False, stopped_at=None) == "online"
    assert _status_cell(online=False, identity_mismatch=False, stopped_at=stopped) == "stopped"
    assert _status_cell(online=False, identity_mismatch=False, stopped_at=None) == "offline"
    # online + a stop marker is the two sources of truth disagreeing, not a green
    # host — see tests/cli/test_rollout_robustness.py for why that mattered.
    assert _status_cell(online=True, identity_mismatch=False, stopped_at=stopped) == "STALE-STOP"


def test_cmd_cluster_status_renders_identity_mismatch_and_code_drift(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row flagged identity_mismatch shows MISMATCH; a row whose running_sha
    differs from head_sha shows the ⚠ drift marker in the new `code` column."""
    roster = [
        _machine_row(
            name="impostor",
            serve_gateway=False,
            serve_agent_runner=True,
            online=False,
            identity_mismatch=True,
        ),
        _machine_row(
            name="stale",
            serve_gateway=False,
            serve_agent_runner=True,
            on_pin=True,
            head_sha="abc1234def",
            running_sha="999888777",
        ),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "code" in out  # new column header
    assert "MISMATCH" in out
    assert "⚠ 9998887" in out


def test_cmd_cluster_status_renders_pin_and_role_columns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The roster has a `pin` column (✓/✗ from each row's on_pin) and a `role`
    column derived from the serve_gateway/serve_agent_runner capability flags
    (regression for the KeyError('role') crash)."""
    roster = [
        _machine_row(
            name="cloud",
            serve_gateway=True,
            serve_agent_runner=True,
            on_pin=True,
            head_sha="abc1234def",
        ),
        _machine_row(
            name="wsl",
            serve_gateway=False,
            serve_agent_runner=True,
            on_pin=False,
            head_sha="999888777",
        ),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "pin" in out
    assert "✓ abc1234" in out
    assert "✗ 9998887" in out
    cloud_line = next(line for line in out.splitlines() if line.startswith("cloud"))
    wsl_line = next(line for line in out.splitlines() if line.startswith("wsl"))
    assert "gateway + agent-runner" in cloud_line
    assert "agent-runner" in wsl_line and "gateway +" not in wsl_line


def test_cmd_cluster_status_role_column_shows_observability_station(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pure observability-station host renders as "observability-station" in
    the roster's role column (WP2 — previously the third capability flag was
    not on the wire and such a host read as "none")."""
    roster = [
        _machine_row(
            name="station-a",
            serve_gateway=False,
            serve_agent_runner=False,
            serve_observability_station=True,
        ),
        _machine_row(
            name="combo",
            serve_gateway=True,
            serve_agent_runner=False,
            serve_observability_station=True,
        ),
        _machine_row(
            name="runner-a",
            serve_gateway=False,
            serve_agent_runner=True,
            serve_observability_station=False,
        ),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    station_line = next(line for line in out.splitlines() if line.startswith("station-a"))
    combo_line = next(line for line in out.splitlines() if line.startswith("combo"))
    runner_line = next(line for line in out.splitlines() if line.startswith("runner-a"))
    assert "observability-station" in station_line and "gateway" not in station_line
    assert "gateway + observability-station" in combo_line
    # Zero regression: a pure runner row never picks up the station token.
    assert "agent-runner" in runner_line
    assert "observability-station" not in runner_line


def test_cmd_status_gateway_cluster_serves_line_shows_station(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava status`'s gateway cluster-status supplement renders the station
    capability in the serves: line when the gateway snapshot carries it
    (the function imports _fetch_gateway_cluster_status at call time, so the
    module attribute patch is the rebind that takes effect)."""
    monkeypatch.setattr(
        "cli.commands.cluster._fetch_gateway_cluster_status",
        lambda: {
            "machine_name": "station-a",
            "serve_gateway": False,
            "serve_agent_runner": False,
            "serve_observability_station": True,
            "paused": False,
        },
    )
    from cli.commands.status import _print_gateway_cluster_status

    _print_gateway_cluster_status()
    out = capsys.readouterr().out
    assert "machine_name: station-a" in out
    assert "serves:       observability-station" in out


# ─── ava cluster status transport failures report, they do not traceback ──────────


def test_cmd_cluster_status_read_timeout_reports_friendly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gateway that does not answer within the probe budget prints one stderr
    line and exits 1. An unreachable machine is exactly when an operator runs
    `ava cluster status`, and its roster probe can push the gateway's own
    response past this client's budget — so a bare ReadTimeout traceback would
    hide the diagnosis the command exists for (#219)."""
    import httpx

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    def _slow_get(url: str, **_kw: object) -> None:
        raise httpx.ReadTimeout("timed out", request=None)

    monkeypatch.setattr("httpx.get", _slow_get)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "did not respond within" in err
    assert "http://gw:8000/api/cluster/roster" in err


def test_cmd_cluster_status_connect_error_reports_friendly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gateway that refuses the connection prints 'gateway unreachable' and
    exits 1 instead of raising (#219)."""
    import httpx

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    def _refused_get(url: str, **_kw: object) -> None:
        raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr("httpx.get", _refused_get)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "gateway unreachable" in err
    assert "connection refused" in err


def test_cmd_cluster_status_http_error_reports_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-2xx roster response names the status code and exits 1 (#219)."""
    import httpx

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    def _server_error_get(url: str, **_kw: object) -> httpx.Response:
        # A real dial returns the 500 response; raise_for_status() in
        # cmd_cluster_status turns it into HTTPStatusError.
        request = httpx.Request("GET", url)
        return httpx.Response(500, request=request)

    monkeypatch.setattr("httpx.get", _server_error_get)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 500" in err


def test_cmd_cluster_status_unresolvable_gateway_reports_friendly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A host that cannot resolve the gateway URL says why and exits 1 rather
    than raising GatewayApiBaseMissing (#219)."""
    from shared.machine import GatewayApiBaseMissing

    monkeypatch.setattr(
        "shared.machine.gateway_api_base",
        lambda: (_ for _ in ()).throw(GatewayApiBaseMissing("AVA_GATEWAY_URL unset")),
    )
    rc = _cli.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot resolve gateway URL" in err


# ─── probe gateway via HTTP, not relying on pidfile ───────────────────────────────────


def _spec_by_service(service: str) -> _cli.ServiceSpec:
    """Look up a ServiceSpec by bare service name."""
    for spec in _cli.build_services():
        if spec.session == service:
            return spec
    raise AssertionError(f"no ServiceSpec service={service!r}")


def test_gateway_spec_uses_http_probe_not_pidfile() -> None:
    """Gateway uvicorn(reload=True) makes pidfile-based liveness unreliable — must probe HTTP.

    `_probe_service` prefers the identity probe, then curl_url, and only falls back
    to the pidfile when neither is set. The gateway spec must set curl_url,
    otherwise `ava status` / the watchdog healthcheck would probe the pidfile —
    which the reload supervisor's fork makes wrong, reporting a healthy gateway dead.
    """
    spec = _spec_by_service("gateway")
    assert spec.curl_url is not None, "gateway must use curl probe (uvicorn reload)"
    assert spec.curl_url.startswith("http://"), f"curl_url shape wrong: {spec.curl_url!r}"


def test_probe_gateway_takes_the_identity_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_probe_service(gateway_spec)` asks the identity probe, not a bare curl.

    The gateway declares one (`probe_home` — 2xx AND this unit's `$AVA_HOME`), so
    the operator surface and the watchdog ask the same question of the same port.
    A plain 2xx would still be satisfied by another cluster's gateway."""
    spec = _spec_by_service("gateway")
    monkeypatch.setattr(
        _cli,
        "_curl_ok",
        lambda _u: pytest.fail("gateway must not fall back to a bare 2xx"),  # pyright: ignore[reportUnknownArgumentType]
    )
    from shared.daemon_health import DaemonProbe

    monkeypatch.setattr(
        "shared.daemon_health._probe_home",
        lambda *_a, **_kw: DaemonProbe.up("home /x"),  # pyright: ignore[reportUnknownArgumentType]
    )
    probe = _cli._probe_service(spec)
    assert probe.alive is True
    assert probe.label == "identity"


def test_probe_gateway_reports_which_fact_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ✗ carries the reason. "down" and "answering, but it is another cluster's
    home" call for completely different actions, and this row is where an operator
    learns which one they have."""
    spec = _spec_by_service("gateway")
    from shared.daemon_health import DaemonProbe

    monkeypatch.setattr(
        "shared.daemon_health._probe_home",
        lambda *_a, **_kw: DaemonProbe.port_taken("identity mismatch: home='/home/ava/.ava'"),  # pyright: ignore[reportUnknownArgumentType]
    )
    probe = _cli._probe_service(spec)
    assert probe.alive is False
    assert "/home/ava/.ava" in probe.detail


def test_probe_survives_an_identity_probe_that_raises() -> None:
    """A plugin's non-total `identity_probe` reports ✗ — it does not take `ava status`
    down with it.

    The three built-in probes convert every failure mode into a `DaemonProbe`;
    a plugin-registered one is under no such obligation. `ava status` is what an
    operator runs when the unit is ALREADY misbehaving, so one plugin raising must
    cost that plugin's row and nothing else."""
    import dataclasses

    def _boom() -> object:
        raise RuntimeError("no socket for you")

    spec = dataclasses.replace(_spec_by_service("gateway"), identity_probe=_boom)
    probe = _cli._probe_service(spec)
    assert probe.alive is False
    assert probe.label == "identity"
    # The type AND the message: "the probe is broken" and "the daemon is down" are
    # different problems, and a fixed string would have made them look alike.
    assert "RuntimeError" in probe.detail
    assert "no socket for you" in probe.detail


def test_register_gateway_advertises_without_gateway_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """gateway registration no longer requires AVA_GATEWAY_URL.

    WP4 (conventions/reachability-and-credentials.md): the advertised URL is
    built on `reachable_host()` — the machine's private-network address — not
    on the bare gateway URL, so a gateway unit with a reachable identity
    registers a dialable address even before `AVA_GATEWAY_URL` is configured
    (a loopback gateway_url advertisement was what made the page proxy refuse
    the host's page servers, the 2026-08-30 serve 400). The port falls back to
    the gateway bind-port setting.

    The real `_register_machine_or_die` is used (autouse fixture replaces the
    module attribute with a noop; `_real_register_machine_or_die` captures the
    original at import time)."""
    from shared.config import settings

    calls: list[str | None] = []

    def fake_register_self(*, url: str | None = None) -> None:
        calls.append(url)

    monkeypatch.setattr("shared.machines.register_self", fake_register_self)
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr(settings.gateway, "gateway_port", 8000)
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.2")
    monkeypatch.setattr("shared.machine.ava_home", lambda: tmp_path)

    rc = _real_register_machine_or_die(
        cast(SetupValues, {"machine_name": "control"}), frozenset({"gateway"})
    )
    assert rc == 0
    assert calls == ["http://10.0.0.2:8000"]


def test_register_gateway_only_advertises_reachable_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A gateway-only unit advertises `reachable_host` + the gateway URL's port —
    NOT the bare gateway URL (WP4: the hostname is what the page proxy's SSRF
    allowlist consumes; a loopback advertisement breaks page serves)."""
    from shared.config import settings

    calls: list[str | None] = []

    def fake_register_self(*, url: str | None = None) -> None:
        calls.append(url)

    monkeypatch.setattr("shared.machines.register_self", fake_register_self)
    monkeypatch.setattr(settings.gateway, "gateway_url", "")
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.2")
    monkeypatch.setattr("shared.machine.ava_home", lambda: tmp_path)
    (tmp_path / "gateway_url").write_text("https://ava.example:8000")

    rc = _real_register_machine_or_die(
        cast(SetupValues, {"machine_name": "control"}), frozenset({"gateway"})
    )
    assert rc == 0
    assert calls == ["http://10.0.0.2:8000"]


def test_register_agent_runner_advertises_ops_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent-runner registers its reachable ops server URL — the exact string the
    gateway later dials. Shape: http://<reachable-host>:<ops_port>."""
    calls: list[str | None] = []

    def fake_register_self(*, url: str | None = None) -> None:
        calls.append(url)

    monkeypatch.setattr("shared.machines.register_self", fake_register_self)
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "10.0.0.2")
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda name: 8106 if name == "ops" else 0,  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _real_register_machine_or_die(
        cast(SetupValues, {"machine_name": "wsl"}), frozenset({"agent-runner"})
    )
    assert rc == 0
    assert calls == ["http://10.0.0.2:8106"]


def test_register_agent_runner_loopback_host_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A remote agent-runner whose reachable address resolves to loopback must fail
    loud (exit 1): register_self raises LoopbackDialUrlRefused rather than writing a
    self-dialing localhost ops URL that a remote gateway would dial itself."""
    from shared.machines import LoopbackDialUrlRefused

    calls: list[str | None] = []

    def _reject(*, url: str | None = None) -> None:
        calls.append(url)
        raise LoopbackDialUrlRefused(f"loopback dial url refused: {url}")

    monkeypatch.setattr("shared.machines.register_self", _reject)
    monkeypatch.setattr("shared.machine.reachable_host", lambda: "127.0.0.1")
    monkeypatch.setattr(
        "shared.daemon_health.health_port",
        lambda name: 8106 if name == "ops" else 0,  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _real_register_machine_or_die(
        cast(SetupValues, {"machine_name": "wsl"}), frozenset({"agent-runner"})
    )
    assert rc == 1
    # register_self was reached with the loopback URL and rejected it; the caller
    # translated that into a non-zero exit rather than a persisted dead row.
    assert calls == ["http://127.0.0.1:8106"]


# ─── multi-machine update orchestration (PR-A) ───────────────────────────────


def test_update_posts_rollout_to_gateway_from_any_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava cluster update` POSTs /api/cluster/rollout to the gateway from ANY
    host — no machine_role() branch (user ruling 2026-08-21, issue #216). The
    body carries origin (default cli:<machine>), mode, force; the response's
    session/log are printed for polling."""
    from typing import cast

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    calls: list[tuple[str, dict[str, object]]] = []

    class _Resp:
        status_code = 202

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"session": "ava-rollout", "log": "/var/log/u.log", "backend_changed": True}

    def _fake_post(url: str, **_kw: object) -> _Resp:
        calls.append((url, cast(dict[str, object], _kw.get("json"))))
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_update()
    assert rc == 0
    assert calls[0][0] == "http://gw:8000/api/cluster/rollout"
    body = calls[0][1]
    assert cast(str, body["origin"]).startswith("cli:")
    assert cast(str, body["mode"]) == "smooth"
    assert body["force"] is False
    assert "ava-rollout" in capsys.readouterr().out


def test_update_restart_only_posts_restart_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava cluster update --restart-only` POSTs /api/cluster/restart (bounce
    on current code) — also from any host, no role branch."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    calls: list[str] = []

    class _Resp:
        status_code = 202

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"session": "ava-rollout", "log": "/var/log/u.log"}

    def _fake_post(url: str, **_kw: object) -> _Resp:
        calls.append(url)
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_update(restart_only=True)
    assert rc == 0
    assert calls == ["http://gw:8000/api/cluster/restart"]
    assert "ava-rollout" in capsys.readouterr().out


def test_gateway_local_update_starts_in_fresh_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_run_gateway_local_update` runs start as a FRESH `ava` subprocess
    (not in-process cmd_start, which mixes stale pre-pull modules with freshly-
    imported ones and crashes on a large jump). The fresh `ava start` applies
    pending migrations itself early in boot — there is no separate migrate step.
    Order: stop -> force-checkout target_sha -> uv sync -> grafana provisioning
    sync (new-tree venv subprocess) -> `ava start`."""
    from cli.commands import update as _up

    repo = tmp_path
    calls: list[str] = []
    cmds: list[list] = []

    monkeypatch.setattr(_up, "_do_stop", lambda *_a, **_kw: calls.append("stop") or 0)  # pyright: ignore[reportUnknownArgumentType]

    # The orchestration created the recovery anchor before entering the local leg.
    monkeypatch.setattr(_up, "git_checkout_sha", lambda _sha: calls.append("checkout") or "aaaaaaa")  # pyright: ignore[reportUnknownArgumentType]

    def _no_inprocess_migrate():
        raise AssertionError("apply_pending_migrations ran in-process")

    monkeypatch.setattr(_up, "apply_pending_migrations", _no_inprocess_migrate)
    # `cmd_start` is no longer imported into the update module — the start runs
    # as a fresh `ava` subprocess, asserted via the subprocess sequence below.
    # The uv sync itself runs through the production sync seam (run_uv_sync ->
    # run_bounded), not subprocess.run, so it is recorded separately.

    def _fake_sync(_repo: Path, *, timeout_s: float = 600.0) -> _FakeResult:
        calls.append("uv-sync")
        return _FakeResult(returncode=0)

    def _passing_import_gate(
        _repo: Path,
        *,
        allowed_roots: Iterable[Path] = (),
    ) -> tuple[str, ...]:
        return ()

    def _fake_run(cmd, *_a, **_kw):
        cmds.append(list(cmd))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return _FakeResult(returncode=0)

    monkeypatch.setattr(_update_uv_sync, "run_uv_sync", _fake_sync)
    monkeypatch.setattr(_update_uv_sync, "editable_import_gate", _passing_import_gate)
    monkeypatch.setattr(_up.subprocess, "run", _fake_run)  # pyright: ignore[reportUnknownArgumentType]

    rc = _up._run_gateway_local_update(
        repo,
        target_sha="bbbbbbb",
        pull_recover=("aaaaaaa", {"00000000T000000_baseline"}, None),
    )
    assert rc == 0
    assert calls == ["stop", "checkout", "uv-sync"]
    # uv sync runs through the production sync seam (run_uv_sync, recorded above),
    # then the fresh `ava start` via subprocess.run (the start no longer needs a
    # pty — the session PATH is forwarded authoritatively, so a plain
    # subprocess.run from the detached rollout works). --persist-services keeps this
    # internal restart from rewriting the operator's durable --disable-service marker.
    # Admission stays held until the orchestration unpauses this host, so agents
    # cannot resume before gateway readiness and Phase B.
    # --no-readiness-gate: this leg's readiness question is answered at step 6.5 by the
    # off-box gateway gate, so the child must not also gate (and must not send a slow
    # non-gateway service into _recover_rc's rollback). See
    # tests/cli/test_start_readiness_gate.py.
    # Repo-native skills refresh on the just-landed tree (issue #1289), also as a
    # fresh subprocess so it runs the new revision's update table.
    assert cmds[0][0].endswith(".venv/bin/ava")  # pyright: ignore[reportUnknownMemberType]
    assert cmds[0][1:] == ["skill", "update"]
    # No Grafana provisioning sync step: the LGTM Grafana container mounts
    # deploy/lgtm/config/grafana/provisioning straight from the checkout,
    # so the checkout above already refreshed it.
    assert cmds[1][0].endswith(".venv/bin/ava")  # pyright: ignore[reportUnknownMemberType]
    assert cmds[1][1:] == [
        "start",
        "--persist-services",
        "--no-readiness-gate",
    ]


def test_update_local_runs_in_process_orchestration(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava cluster update --local` — the explicit escape hatch the detached
    ava-rollout session runs — dispatches to `_run_gateway_orchestration` in
    this foreground process. No role read: the user asked for the local leg on
    whatever host they are on."""
    from cli.commands import update as _up_mod

    monkeypatch.setattr(_up_mod, "_repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(_up_mod, "ava_home", lambda: Path("/home"))
    monkeypatch.setattr(_up_mod, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]

    def _no_post(*_a: object, **_kw: object) -> None:
        raise AssertionError("--local must not POST the gateway")

    monkeypatch.setattr("httpx.post", _no_post)  # pyright: ignore[reportUnknownArgumentType]
    calls: list[str] = []

    def _orch(_repo, **_kw):
        calls.append("orchestration")
        return 0

    monkeypatch.setattr(_cli, "_run_gateway_orchestration", _orch)  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli.cmd_update(local=True)
    assert rc == 0
    assert calls == ["orchestration"]


def test_cmd_start_returns_this_host_to_idle_posture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`ava start` at the end writes the idle posture row — if a previous update
    left the host paused, a manual ava start recovers (no need to ssh + rm). The
    old cluster_paused file was retired with the old-signal sweep (PR5)."""
    calls: list[str] = []
    monkeypatch.setattr("shared.host_deploy_state.set_posture", calls.append)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli.cmd_start()
    assert rc == 0
    assert calls and calls[-1] == "idle"


def test_cmd_start_finalizes_a_paused_deploy_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression (2026-08-26): a Phase-B `ava start` restores posture without a
    cluster/resume op, so the pause-owner journal must be finalized (paused ->
    resumed, generation preserved) by the start itself — otherwise it stays
    `paused` forever while the host serves (rollout rc=0, deploy-pause-owner.json
    still paused). The exact journaled generation must be kept, so a delayed
    resume for that generation stays an idempotent no-op and a foreign one is
    refused."""
    from datetime import UTC, datetime

    from shared import pause_owner

    owner_path = tmp_path / "deploy-pause-owner.json"
    lock_path = tmp_path / "deploy-pause-owner.lock"
    monkeypatch.setattr(pause_owner, "state_path", lambda: owner_path)
    monkeypatch.setattr(pause_owner, "lock_path", lambda: lock_path)
    monkeypatch.setattr(
        "shared.host_deploy_state.set_posture",
        lambda _p: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    acquired = datetime(2026, 8, 26, 14, 14, 42, tzinfo=UTC)
    pause_owner.mark_paused("macmini:pid65276", acquired)

    def ready(*_args: object, **_kwargs: object) -> _cli.ReadinessWait:
        assert pause_owner.read().status == "paused", "finalize must wait for readiness"
        return _cli.ReadinessWait((), 0.0, sessions_gone=False)

    monkeypatch.setattr(_cli, "_wait_for_services_ready", ready)
    assert _cli.cmd_start() == 0

    snapshot = pause_owner.read()
    assert snapshot.status == "resumed"
    assert snapshot.matches("macmini:pid65276", acquired)
    # The finalize kept the exact generation: the same-generation resume stays an
    # idempotent no-op, a delayed foreign resume stays refused.
    assert pause_owner.mark_resumed("macmini:pid65276", acquired)
    assert not pause_owner.mark_resumed("other:pid1", acquired)


def test_rollout_child_start_does_not_finalize_the_pause_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A rollout child (gateway local leg) leaves posture `converging` and the
    admission held — the orchestrator's own finally owns that resume boundary, so
    the start must not record the pause as completed while the host is still
    mid-transition."""
    from datetime import UTC, datetime

    from cli.commands import _data_plane_admin_secrets as secrets_mod
    from cli.commands import start as start_mod
    from shared import pause_owner
    from shared.cluster_lock import DeployLease

    owner_path = tmp_path / "deploy-pause-owner.json"
    lock_path = tmp_path / "deploy-pause-owner.lock"
    monkeypatch.setattr(pause_owner, "state_path", lambda: owner_path)
    monkeypatch.setattr(pause_owner, "lock_path", lambda: lock_path)
    monkeypatch.setattr(
        "shared.host_deploy_state.set_posture",
        lambda _p: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: DeployLease(
            holder="rollout:42",
            held_for_s=10,
            expires_in_s=900,
            note=None,
            kind="rollout",
            acquired_at=datetime(2026, 8, 26, 14, 14, 42, tzinfo=UTC),
        ),
    )
    monkeypatch.setattr(
        secrets_mod,
        "ensure_data_plane_admin_secrets",
        lambda **_kw: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(start_mod, "cmd_status", lambda: 0)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    pause_owner.mark_paused("rollout:42", datetime(2026, 8, 26, 14, 14, 42, tzinfo=UTC))

    assert _cli.cmd_start(persist_services=False) == 0

    snapshot = pause_owner.read()
    assert snapshot.status == "paused"
    assert snapshot.matches("rollout:42", datetime(2026, 8, 26, 14, 14, 42, tzinfo=UTC))


def test_rollout_child_keeps_converging_before_parent_readiness(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """An old parent has no handoff marker, so its executing lease is the
    compatibility proof: the fresh internal start must not revive agents."""
    from cli.commands import _data_plane_admin_secrets as secrets_mod
    from cli.commands import start as start_mod
    from shared.cluster_lock import DeployLease
    from shared.rollout_handoff import ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV

    service, _shell = _fake_session_backends
    postures: list[str] = []
    legacy_upgrade: list[bool] = []

    def _record_legacy_upgrade(*, allow_legacy_upgrade: bool) -> bool:
        legacy_upgrade.append(allow_legacy_upgrade)
        return False

    monkeypatch.delenv(ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV, raising=False)
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: DeployLease(
            holder="old-parent:42",
            held_for_s=10,
            expires_in_s=900,
            note=None,
            kind="rollout",
        ),
    )
    monkeypatch.setattr("shared.host_deploy_state.set_posture", postures.append)
    monkeypatch.setattr(
        secrets_mod,
        "ensure_data_plane_admin_secrets",
        _record_legacy_upgrade,
    )
    monkeypatch.setattr(start_mod, "cmd_status", lambda: 0)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli.cmd_start(persist_services=False)

    assert rc == 0
    assert postures[-1] == "converging"
    assert _sess("restarter") not in service.created
    assert legacy_upgrade == [False]


def test_handoff_capable_rollout_child_may_commit_credential_transition(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """The follow-up rollout carries v1 proof: credential mutation becomes
    legal while admission remains behind the same resume boundary."""
    from cli.commands import _data_plane_admin_secrets as secrets_mod
    from cli.commands import start as start_mod
    from shared.rollout_handoff import (
        ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV,
        ROLLOUT_PARENT_CREDENTIAL_HANDOFF_VERSION,
    )

    service, _shell = _fake_session_backends
    legacy_upgrade: list[bool] = []

    def _record_legacy_upgrade(*, allow_legacy_upgrade: bool) -> bool:
        legacy_upgrade.append(allow_legacy_upgrade)
        return False

    monkeypatch.setenv(
        ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV,
        ROLLOUT_PARENT_CREDENTIAL_HANDOFF_VERSION,
    )
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: pytest.fail("the versioned parent marker is authoritative"),
    )
    monkeypatch.setattr(
        secrets_mod,
        "ensure_data_plane_admin_secrets",
        _record_legacy_upgrade,
    )
    monkeypatch.setattr(start_mod, "cmd_status", lambda: 0)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _cli.cmd_start(persist_services=False) == 0
    assert legacy_upgrade == [True]
    assert _sess("restarter") not in service.created
    assert ROLLOUT_PARENT_CREDENTIAL_HANDOFF_ENV not in os.environ


def test_phase_b_pure_runner_restores_idle_posture_and_agent_host(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """Phase B uses the same internal ``ava start --persist-services`` shape
    under the executing cluster lease, but a pure runner must finish its local
    transition instead of inheriting the gateway parent's resume boundary."""
    from cli.commands import start as start_mod
    from shared.cluster_lock import DeployLease

    service, _shell = _fake_session_backends
    postures: list[str] = []
    monkeypatch.setattr(
        "shared.machine.machine_role",
        lambda: frozenset({"agent-runner"}),
    )
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: DeployLease(
            holder="gateway-rollout:42",
            held_for_s=10,
            expires_in_s=900,
            note=None,
            kind="rollout",
        ),
    )
    monkeypatch.setattr("shared.host_deploy_state.set_posture", postures.append)
    monkeypatch.setattr(start_mod, "cmd_status", lambda: 0)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _cli.cmd_start(persist_services=False) == 0
    assert postures[-1] == "idle"
    assert _sess("agent-host") in service.created


def test_operator_start_refuses_executing_rollout_before_migrations(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """A concurrent operator cannot become a second schema writer."""
    from cli.commands import start as start_mod
    from shared.cluster_lock import DeployLease

    service, _shell = _fake_session_backends
    monkeypatch.setattr(
        "shared.cluster_lock.read_update_lease",
        lambda: DeployLease(
            holder="rollout:42",
            held_for_s=10,
            expires_in_s=900,
            note=None,
            kind="rollout",
        ),
    )
    monkeypatch.setattr(
        start_mod,
        "cmd_migrations_apply",
        lambda: pytest.fail("migration ran before rollout refusal"),
    )

    assert _cli.cmd_start() == 1
    assert service.created == []


def test_rollout_lease_read_failure_is_before_migrations(
    monkeypatch: pytest.MonkeyPatch,
    _fake_session_backends: tuple[_FakeSessionBackend, _FakeSessionBackend],
) -> None:
    """An unreadable rollout authority fails closed before schema mutation."""
    from cli.commands import start as start_mod

    service, _shell = _fake_session_backends

    def _unreadable() -> None:
        raise RuntimeError("lease unavailable")

    monkeypatch.setattr("shared.cluster_lock.read_update_lease", _unreadable)
    monkeypatch.setattr(
        start_mod,
        "cmd_migrations_apply",
        lambda: pytest.fail("migration ran with unreadable rollout authority"),
    )

    assert _cli.cmd_start() == 1
    assert service.created == []


def test_pending_credential_transition_replays_before_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash journal is adopted before the first schema client is opened."""
    from cli.commands import _data_plane_admin_secrets as secrets_mod
    from cli.commands import start as start_mod

    order: list[str] = []
    monkeypatch.setattr(
        secrets_mod,
        "resume_pending_data_plane_admin_secrets",
        lambda: order.append("resume"),
    )
    monkeypatch.setattr(
        start_mod,
        "cmd_migrations_apply",
        lambda: order.append("migrate") or 0,
    )
    monkeypatch.setattr(start_mod, "cmd_status", lambda: 0)
    monkeypatch.setattr("shared.cluster_lock.read_update_lease", lambda: None)
    monkeypatch.setattr(
        _cli.subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _cli.cmd_start() == 0
    assert order[:2] == ["resume", "migrate"]


def test_machine_description_setup_field_writes_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cli.commands import _setup

    monkeypatch.setattr(settings.general, "machine_description", "")
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    field = next(f for f in _setup._SETUP_FIELDS if f.name == "machine_description")
    # arg provided → write file + return value
    assert _setup._resolve_setup_field(field, "voice IO + browser") == "voice IO + browser"
    assert (tmp_path / "machine_description").read_text() == "voice IO + browser"
    # no env, no file, no arg → optional field returns None
    no_file_tmp = tmp_path / "subdir_no_file"
    no_file_tmp.mkdir()
    monkeypatch.setattr("shared.paths.ava_home", lambda: no_file_tmp)
    assert _setup._resolve_setup_field(field, None) is None


def test_fan_out_classifies_dispatch_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    """_dispatch_one_and_wait maps direct-dial outcomes to the (ok / fatal /
    unreachable) triplet that upstream print/abort logic still depends on."""
    from ops import cluster_rpc as cr

    async def _ok(*_a, **_kw):
        return {}

    async def _unreachable(*_a, **_kw):
        raise cr.ClusterOpUnreachable("simulated")

    async def _fail(*_a, **_kw):
        raise cr.ClusterOpFailed({"error": "agent-runner blew up"})

    # ok
    monkeypatch.setattr(cr, "dispatch_to_machine", _ok)  # pyright: ignore[reportUnknownArgumentType]
    name, status, _ = asyncio.run(_cli._dispatch_one_and_wait("wsl", "cluster_stop", 5.0))
    assert (name, status) == ("wsl", "ok")

    # unreachable ops server
    monkeypatch.setattr(cr, "dispatch_to_machine", _unreachable)  # pyright: ignore[reportUnknownArgumentType]
    name, status, detail = asyncio.run(_cli._dispatch_one_and_wait("wsl", "cluster_stop", 5.0))
    assert (name, status) == ("wsl", "unreachable")
    assert "unreachable" in detail

    # op ran but failed -> fatal
    monkeypatch.setattr(cr, "dispatch_to_machine", _fail)  # pyright: ignore[reportUnknownArgumentType]
    name, status, detail = asyncio.run(_cli._dispatch_one_and_wait("wsl", "cluster_stop", 5.0))
    assert (name, status) == ("wsl", "fatal")
    assert "agent-runner blew up" in detail


def test_poll_until_unpaused_returns_ok_when_agent_runner_unpauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_poll_until_unpaused` resolves an agent-runner the moment its
    host_deploy_state row returns to idle. The dispatch layer is stubbed to keep
    the probe reachable; the deploy-state reader flips paused -> idle after the
    first probe so the retry loop is exercised, not just the happy-first-shot
    path (R1, Task #1021 — the row, not the probe's `paused` field, is the
    verdict)."""
    from datetime import UTC, datetime

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    calls = {"wsl": 0}

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        assert kind == "status_probe"
        assert payload == {}
        assert retries == 0  # the outer Phase-B loop is the only retry policy
        # the poll threads the pre-resolved ops URL so it never re-queries Postgres
        assert ops_url == "http://unused"
        calls[target_machine] += 1
        return {}

    def _fake_read(machine=None, **_kw):
        posture = "paused" if calls["wsl"] < 2 else "idle"
        return HostDeployState(
            machine=machine or "wsl",  # pyright: ignore[reportUnknownArgumentType]
            posture=posture,
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 5.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("wsl", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"wsl": "ok"}
    assert calls["wsl"] >= 2


def test_poll_until_unpaused_marks_converging_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent-runner whose updater lease stays live until the deadline is
    POLL_CONVERGING — 'the poll ran out of patience', not 'this host stopped'.
    The live lease is the evidence it is still working (R1, Task #1021)."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "wsl",  # pyright: ignore[reportUnknownArgumentType]
            posture="paused",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("wsl", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"wsl": _cli.POLL_CONVERGING}


def test_poll_gives_up_at_once_on_a_host_that_provably_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host whose deploy-state row says `converging` with NO live updater lease
    has lost its updater without resuming (the declined-restart shape: the chain
    touched the lease, the restart refused, and the host never returned to idle).
    More waiting cannot help. The poll returns POLL_STALLED within a couple of
    intervals instead of burning the whole bound — which is what makes a bound
    long enough for a Windows leg affordable. Measured on prod: three
    consecutive rollouts spent the full poll on a host whose updater had exited
    in 3s. R1 (Task #1021): the row is the verdict.
    """
    from datetime import UTC, datetime

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    probes = {"n": 0}

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        probes["n"] += 1
        return {}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "air",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    # A bound this generous would take ~an hour to reach; the assertion is that the
    # verdict does NOT wait for it.
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("air", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"air": _cli.POLL_STALLED}
    # Two consecutive confirmations, not one: a single contrary reading is a
    # spawn/teardown race, not evidence.
    assert probes["n"] == 2


def test_a_previous_updates_uncleared_lease_is_not_this_ones_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The false positive behind two of prod's `win: STALLED` rounds. The pause does
    not clear the lease column, so a run that ended without clearing leaves its expiry
    in the row; the next update's pause then sits in front of it, and for the seconds
    between `pause_local_cluster` and the updater's first touch — a session spawn plus
    a Python cold start, which on a Windows host is the slow part — the row reads
    exactly like a host whose updater died. Two probes later Phase B abandoned a host
    that went on to converge minutes afterwards, and held the cluster for a settle
    window waiting on it."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import UPDATER_LEASE_TTL_S, HostDeployState

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {}

    now = datetime.now(UTC)
    expired = now - timedelta(seconds=60)

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="paused",
            updated_at=now,
            # Armed by the PREVIOUS update — a whole TTL before it expired, which is
            # before this pause window opened.
            updater_lease_expires_at=expired,
            paused_at=expired - timedelta(seconds=UPDATER_LEASE_TTL_S) + timedelta(seconds=1),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}


def test_poll_gives_up_on_a_written_verdict_the_stale_lease_contradicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lease is one write at the run's start, armed for the same 15 minutes this
    poll is willing to wait — so every way of never reaching its clear step (a cmd.exe
    chain that `exit /b`s first, a clear that cannot reach the DB through the restart
    it is part of, a killed process) leaves a host that stopped in seconds claiming to
    be busy for the whole bound. Its own written ending outranks the claim: the updater
    said it finished, and the posture row says it did not converge."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    probes = {"n": 0}

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        probes["n"] += 1
        return {
            "last_updater_outcome": {
                "kind": "exited",
                "rc": 1,
                "detail": "[updater] checkout/sync or tree verification FAILED",
                "log": "ava-updater.out.log",
            },
        }

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            # The lease the abort never cleared: still minutes from expiring.
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("win", "http://unused")])

    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_STALLED}
    assert probes["n"] == 2  # the same two confirmations, not one
    assert out["win"].updater is not None
    assert out["win"].updater["rc"] == 1


def test_a_live_lease_with_no_written_ending_still_keeps_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard on the short-cut above, and the reason it is keyed on a *terminal*
    outcome rather than on there being one at all: a host mid-`uv sync` has a log and
    a live lease and is working. `unknown` is what its log says, and abandoning it
    would trade a slow rollout for a broken host."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {"last_updater_outcome": {"kind": "unknown", "rc": None, "log": "x.log"}}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}


def test_a_live_lease_with_stuck_stage_evidence_is_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The P1 (2026-08-30) shape: the updater lease is one write at the run's start,
    so a host hung inside `uv` (a stalled network download on the Windows runner)
    used to read "still working" for the whole 900 s bound while its own stage
    evidence showed nothing completing. Two consecutive probes naming the same
    stage in flight beyond STAGE_NO_PROGRESS_TIMEOUT_S end the poll with
    POLL_NO_PROGRESS — the lease stays live, but the progress fact outranks the
    claim."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    probes = {"n": 0}

    async def _stuck(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        probes["n"] += 1
        return {
            "last_updater_outcome": {
                "kind": "unknown",
                "rc": None,
                "log": "ava-updater.out.log",
                "current_stage": "uv",
                "current_stage_s": 700.0,
            },
        }

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _stuck)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused([("win", "http://unused")])

    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_NO_PROGRESS}
    assert probes["n"] == 2  # the same two confirmations, not one
    assert out["win"].updater is not None
    assert out["win"].updater["current_stage"] == "uv"


def test_other_hosts_converged_while_one_is_stuck_in_a_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll is per host: a stuck Windows box must not drag the hosts that
    converged. The two healthy hosts return ok at once; the stuck one returns
    no_progress as soon as its evidence proves it — the poll does not wait out the
    whole bound for it, and the caller's settle hold covers exactly the stuck
    host."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    calls = {"air": 0, "mini": 0, "win": 0}

    async def _probe(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        calls[target_machine] += 1
        if target_machine == "win":
            return {
                "last_updater_outcome": {
                    "kind": "unknown",
                    "rc": None,
                    "log": "ava-updater.out.log",
                    "current_stage": "uv",
                    "current_stage_s": 700.0,
                },
            }
        return {}

    def _fake_read(machine=None, **_kw):
        if machine == "win":
            posture = "converging"
            lease = datetime.now(UTC) + timedelta(seconds=800)
        else:
            posture = "idle"
            lease = None
        return HostDeployState(
            machine=machine or "air",  # pyright: ignore[reportUnknownArgumentType]
            posture=posture,
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=lease,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _probe)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused(
        [("air", "http://unused"), ("mini", "http://unused"), ("win", "http://unused")]
    )

    assert {n: v.status for n, v in out.items()} == {
        "air": _cli.POLL_OK,
        "mini": _cli.POLL_OK,
        "win": _cli.POLL_NO_PROGRESS,
    }
    # The stuck host ends the poll on its own verdict; the converged hosts needed
    # exactly one probe each and the stuck one its two confirmations.
    assert calls["air"] == 1
    assert calls["mini"] == 1
    assert calls["win"] == 2


def test_stage_evidence_that_advances_keeps_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stage in flight BELOW the bound, or a stage that changes between probes
    (progress), is working — the no-progress streak must not fire. A slow Windows
    uv leg is exactly this shape: the same stage name, an age that keeps growing,
    and the poll's own deadline remains the patience."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    async def _young_stage(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {
            "last_updater_outcome": {
                "kind": "unknown",
                "rc": None,
                "log": "updater-178.log",
                "current_stage": "uv",
                "current_stage_s": 100.0,
            },
        }

    monkeypatch.setattr(cr, "dispatch_to_machine", _young_stage)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}


def test_continuous_progress_past_the_converging_bound_hands_the_host_to_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C3: a host that is ALIVE AND MAKING PROGRESS must not burn the whole 900 s
    poll bound — 340 probes on 2026-08-30's wsl runner — when the settle hold +
    watchdog already cover its remaining convergence. Continuous progress past
    `_CONVERGING_TIMEOUT_S` ends the poll early with POLL_CONVERGING (the same
    verdict the deadline produces, so the settle set / report / resume need no
    branching)."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    probes = {"n": 0}

    async def _young_stage(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        probes["n"] += 1
        return {
            "last_updater_outcome": {
                "kind": "unknown",
                "rc": None,
                "log": "updater-178.log",
                "current_stage": "uv",
                "current_stage_s": 100.0,
            },
        }

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "wsl",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _young_stage)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    # A generous absolute deadline, so the ONLY thing that can end the poll is the
    # converging bound — the assertion is that the poll does NOT wait it out.
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_CONVERGING_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused([("wsl", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"wsl": _cli.POLL_CONVERGING}
    # ~6 probes at 0.01 s intervals reach the 0.05 s bound; the early exit must
    # not spend anything like the 3600 s deadline. (Tight bounds would flake on
    # monotonic drift, so assert the order of magnitude.)
    assert probes["n"] < 50


def test_phase_b_deadline_contract_matches_the_timing_invariants() -> None:
    """The public 900-second absolute deadline and C3's 300-second handoff are
    different clocks with one authoritative definition each."""
    from shared.deploy_timing import (
        CONVERGING_POLL_TIMEOUT_S,
        NO_PROGRESS_TIMEOUT_S,
        PHASE_B_ABSOLUTE_TIMEOUT_S,
    )

    assert _cli._POLL_TIMEOUT_S == PHASE_B_ABSOLUTE_TIMEOUT_S == NO_PROGRESS_TIMEOUT_S == 900.0
    assert _cli._CONVERGING_TIMEOUT_S == CONVERGING_POLL_TIMEOUT_S == 300.0
    assert CONVERGING_POLL_TIMEOUT_S < PHASE_B_ABSOLUTE_TIMEOUT_S


def test_a_restart_resets_the_converging_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """C3's patience is the CONTINUOUS progress streak, not total poll time: an
    unreachable reading (the expected mid-restart silence) must reset the clock,
    or a host that restarts midway through its leg would be handed to settle on
    stale progress that predates the restart."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    probes = {"n": 0}

    async def _stop_start(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        probes["n"] += 1
        # One unreachable reading (the mid-restart silence) after two
        # progressing ones: the streak must restart from the silence.
        if probes["n"] == 3:
            raise cr.ClusterOpUnreachable("restarting")
        return {
            "last_updater_outcome": {
                "kind": "unknown",
                "rc": None,
                "log": "updater-178.log",
                "current_stage": "uv",
                "current_stage_s": 100.0,
            },
        }

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "wsl",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _stop_start)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_CONVERGING_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused([("wsl", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"wsl": _cli.POLL_CONVERGING}
    # Without the reset, ~5-6 probes (one 0.05 s streak) would end the poll; the
    # reset at probe 3 forces the second streak to restart, so the poll needs
    # meaningfully more probes before the bound accumulates. The bound is set at
    # 7 with headroom: a slow CI box stretches the sleep intervals and shrinks
    # the probe count (QA nit, PR #1200) — 6 would make the assertion flaky.
    assert probes["n"] >= 7


def test_probe_verdict_names_the_progress_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    """`progressing` is True only for the one shape the converging bound exists
    for — a live lease with stage evidence that is not stuck. A paused pre-lease
    window, a stuck stage's first reading, and an idle posture are not progress."""
    from datetime import UTC, datetime, timedelta

    from cli.commands._update_phase_b import _probe_verdict
    from shared.host_deploy_state import HostDeployState

    def _row(posture: str, lease: bool, stage_age: float | None):
        def _read(machine=None, **_kw):
            return HostDeployState(
                machine=machine or "wsl",  # pyright: ignore[reportUnknownArgumentType]
                posture=posture,
                updated_at=datetime.now(UTC),
                updater_lease_expires_at=(
                    datetime.now(UTC) + timedelta(seconds=800) if lease else None
                ),
            )

        monkeypatch.setattr("cli.commands._update_phase_b.read", _read)  # pyright: ignore[reportUnknownArgumentType]
        probe: dict[str, object] = {}
        if stage_age is not None:
            probe["last_updater_outcome"] = {
                "kind": "unknown",
                "rc": None,
                "log": "x.log",
                "current_stage": "uv",
                "current_stage_s": stage_age,
            }
        return _probe_verdict(probe, 0, "wsl", 0)

    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)
    # A live lease with a young stage is the progressing shape.
    _v, _s, _n, progressing = _row("converging", True, 100.0)
    assert _v is None and progressing is True
    # A live lease with NO stage fields ("cannot tell", older commit) is not
    # progress — it resets the streak like the no-progress rule's polarity.
    _v, _s, _n, progressing = _row("converging", True, None)
    assert _v is None and progressing is False
    # A live lease with a stuck stage starts the no-progress streak instead.
    _v, _s, _n, progressing = _row("converging", True, 700.0)
    assert _v is None and _n == 1 and progressing is False
    # A paused pre-lease window (no lease yet) is "cannot tell", not progress.
    _v, _s, _n, progressing = _row("paused", False, None)
    assert _v is None and progressing is False
    # Idle is convergence, and it is not the progressing shape.
    _v, _s, _n, progressing = _row("idle", False, None)
    assert _v is not None and _v.status == _cli.POLL_OK and progressing is False


def test_paused_without_lease_stalls_once_the_arm_grace_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-09-02 win: the updater's recovery `ava start` exited rc=1 under the
    executing deploy lease, its lease clear ran, and the paused-no-lease reading
    kept the poll "cannot tell" for the whole 900 s bound. Once THIS host's poll
    has run past the lease-arm grace, a paused host with no live lease is
    provably not running an updater — a stall candidate like any other provable
    stop. The grace is measured from the poll's own clock, never `paused_at`:
    the pause (Phase A) and the updater spawn (Phase B trigger) are minutes
    apart by design."""
    from datetime import UTC, datetime, timedelta

    from cli.commands._update_phase_b import _probe_verdict
    from shared.host_deploy_state import HostDeployState

    def _read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="paused",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
            paused_at=datetime.now(UTC) - timedelta(minutes=10),
        )

    monkeypatch.setattr("cli.commands._update_phase_b.read", _read)  # pyright: ignore[reportUnknownArgumentType]
    # Inside the arm grace: still "cannot tell"; neither counter advances.
    verdict, stalls, no_progress, progressing = _probe_verdict({}, 0, "win", 0, poll_elapsed=10.0)
    assert verdict is None and stalls == 0 and no_progress == 0
    assert progressing is False
    # Past the grace: the first stalled reading, then two confirmations end it.
    verdict, stalls, no_progress, _ = _probe_verdict({}, 0, "win", 0, poll_elapsed=91.0)
    assert verdict is None and stalls == 1
    verdict, stalls, no_progress, _ = _probe_verdict({}, 1, "win", 0, poll_elapsed=93.0)
    assert verdict is not None and verdict.status == _cli.POLL_STALLED


def test_a_probe_from_an_older_commit_never_reads_as_no_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner answering from a commit that predates the stage fields reports no
    `current_stage` — cannot tell, never progress, so the poll keeps going to its
    deadline. The judgment applies from the rollout after the one that ships it,
    the same one-rollout lag `last_updater_outcome` itself had."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _older_commit(
        *, target_machine, kind, payload, timeout_s, ops_url=None, retries=None
    ):
        return {"last_updater_outcome": {"kind": "unknown", "rc": None, "log": "x.log"}}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=datetime.now(UTC) + timedelta(seconds=800),
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _older_commit)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(_cli, "_STAGE_NO_PROGRESS_S", 600.0)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}


def test_no_progress_verdict_renders_its_own_next_step() -> None:
    """POLL_NO_PROGRESS is a different fact from CONVERGING and STALLED — the host
    is alive and stuck, and the operator's next move is to look at that machine's
    network, not to wait or to restart it."""
    verdict = _cli.PollVerdict(
        _cli.POLL_NO_PROGRESS,
        {"kind": "unknown", "current_stage": "uv", "current_stage_s": 700.0, "log": "x.log"},
    )
    detail = _poll_verdict_detail(verdict)
    assert "NO PROGRESS" in detail
    assert "uv" in detail
    assert "network" in detail


def test_db_read_failure_keeps_polling_never_reports_converged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB hiccup on the deploy-state row is 'cannot tell', never convergence:
    the old code collapsed the read failure into `state=None` and returned
    POLL_OK, which would release the deploy lease while the host is still
    mid-transition. The poll must keep going to its deadline instead."""
    from ops import cluster_rpc as cr

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {}

    def _broken_read(machine=None, **_kw):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _broken_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("air", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"air": _cli.POLL_CONVERGING}


def test_db_read_failure_is_not_a_stall_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stall counter must not advance on a read failure either: an
    unreadable row is evidence-free, and two 'cannot tell' readings are not a
    provable stop."""
    from cli.commands._update_phase_b import _probe_verdict

    def _broken_read(machine=None, **_kw):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr("cli.commands._update_phase_b.read", _broken_read)  # pyright: ignore[reportUnknownArgumentType]

    verdict, stalls, no_progress, progressing = _probe_verdict({}, 3, "air", 2)
    assert verdict is None
    assert stalls == 3  # counter untouched — a read failure is not a stall observation
    assert no_progress == 2  # same rule: a read failure is not a no-progress observation
    assert progressing is False  # and it is not progress either (C3): the converging
    # clock must reset on evidence-free readings, or a DB hiccup would keep it running


def test_a_stalled_host_carries_its_own_updater_outcome_off_the_box(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reason rides the same probe that settles the verdict, so it costs no extra
    dial: the response that proves the host stopped is the response that says why."""
    from datetime import UTC, datetime

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _declined(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {
            "last_updater_outcome": {
                "kind": "declined",
                "rc": 3,
                "detail": "✗ gateway unreachable at http://gw:8000",
                "log": "updater-1785470000.log",
            },
        }

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "air",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _declined)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    verdict = _cli._poll_until_unpaused([("air", "http://unused")])["air"]

    assert verdict.status == _cli.POLL_STALLED
    assert verdict.updater is not None
    assert verdict.updater["kind"] == "declined"


def test_a_runner_that_never_sent_an_outcome_reports_no_record_not_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host on a commit that predates the field sends no `last_updater_outcome` at
    all — the same silence as a host whose log did not speak for this update. Both
    are 'no record', which the report says outright rather than inventing rc=0."""
    from datetime import UTC, datetime

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _older_commit(
        *, target_machine, kind, payload, timeout_s, ops_url=None, retries=None
    ):
        return {}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "old",  # pyright: ignore[reportUnknownArgumentType]
            posture="converging",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _older_commit)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 3600.0)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    verdict = _cli._poll_until_unpaused([("old", "http://unused")])["old"]

    assert verdict.status == _cli.POLL_STALLED
    assert verdict.updater is None
    assert "no updater record" in _poll_verdict_detail(verdict)


def test_two_stalled_hosts_no_longer_read_identically(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect, stated as a test. A refusal and a death both produce
    `POLL_STALLED`, and the report used to print one sentence for both — so the
    operator's only next step was to ssh in and read a log, on the platform where
    that is hardest and for the case where nothing is actually broken."""
    declined = _cli.PollVerdict(
        _cli.POLL_STALLED,
        {"kind": "declined", "rc": 3, "detail": "✗ gateway unreachable at http://gw:8000"},
    )
    died = _cli.PollVerdict(_cli.POLL_STALLED, {"kind": "unknown", "log": "updater-178.log"})

    declined_line = _poll_verdict_detail(declined)
    died_line = _poll_verdict_detail(died)

    assert declined_line != died_line
    # the refusal says the host is intact and names what the preflight complained of
    assert "still serving its old code" in declined_line
    assert "gateway unreachable at http://gw:8000" in declined_line
    # the death says the opposite thing about the host's state
    assert "still serving its old code" not in died_line
    assert "died mid-flight" in died_line


def test_a_refusal_is_not_told_to_wait_for_its_watchdog() -> None:
    """ "Its watchdog re-triggers the self-update" is true of a death and wrong of a
    refusal — and wrong in the expensive direction, because it reads as "wait" for the
    one case waiting does not clear. A declined host is still paused (rc=3 skips the
    `ava start` that unlinks the flag), `PauseController` blocks the tick ahead of
    `PinController`, and the off-pin heal it blocks converges by POSTing the very
    gateway the preflight refused over."""
    declined = _cli.PollVerdict(_cli.POLL_STALLED, {"kind": "declined", "rc": 3})
    died = _cli.PollVerdict(_cli.POLL_STALLED, {"kind": "exited", "rc": 1})

    declined_line = _poll_verdict_detail(declined)
    died_line = _poll_verdict_detail(died)

    assert "will NOT self-heal" in declined_line
    assert "watchdog re-triggers the self-update" not in declined_line
    # the operator gets something to do instead of something to wait for
    assert "re-run the update" in declined_line
    # a death is still the case the watchdog does handle
    assert "watchdog re-triggers the self-update" in died_line


def test_a_death_is_told_what_has_to_happen_before_its_watchdog_can_act() -> None:
    """Issue #1114. The death branch promised a watchdog re-trigger with no precondition
    — to a host this same poll has just classified as *still paused*. The pause gate
    that holds a refusal's heal back holds a death's heal back identically
    (`PauseController` blocks the whole tick ahead of `PinController`), so the verdict
    was not wrong so much as missing the step it depends on: something has to lift the
    pause first, and in this rollout that is the compensating resume `finalize_rollout`
    is about to fan out."""
    died = _cli.PollVerdict(_cli.POLL_STALLED, {"kind": "exited", "rc": 1})
    line = _poll_verdict_detail(died)

    assert "watchdog re-triggers the self-update" in line
    assert "once the pause is lifted" in line
    assert "compensating resume" in line
    # no duration: the flag stands until nothing owns the pause, and this rollout is
    # about to hold the lease over exactly these hosts.
    assert not re.search(r"\d+\s*(m|min|s|sec)\b", line), line


def test_no_stalled_verdict_quotes_a_recovery_deadline() -> None:
    """Both branches, one rule. A number here would be read as "wait that long", which
    is a promise neither the refusal (waiting never clears it) nor the death (the bound
    depends on who owns the pause) can keep."""
    for updater in (
        {"kind": "declined", "rc": 3},
        {"kind": "exited", "rc": 1},
        {"kind": "unknown", "log": "updater-178.log"},
    ):
        line = _poll_verdict_detail(_cli.PollVerdict(_cli.POLL_STALLED, updater))
        assert not re.search(r"\bin \d+\s*(m|min|s|sec)\b", line), line


def test_poll_stall_verdict_needs_consecutive_confirmations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One `converging`-with-no-lease reading between two live-lease ones is a race,
    not a stall: the counter resets, so the host is never abandoned on a single
    sample."""
    from datetime import UTC, datetime, timedelta

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    readings = ["stuck", "live", "stuck", "live"]
    seen = {"n": 0}

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {}

    def _fake_read(machine=None, **_kw):
        idx = min(seen["n"], len(readings) - 1)
        seen["n"] += 1
        if readings[idx] == "live":
            lease = datetime.now(UTC) + timedelta(seconds=60)
            posture = "converging"
        else:
            lease = None
            posture = "converging"
        return HostDeployState(
            machine=machine or "win",  # pyright: ignore[reportUnknownArgumentType]
            posture=posture,
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=lease,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.08)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}


def test_poll_paused_before_the_first_lease_touch_is_never_a_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paused with NO updater lease is the fan-out window (spawn -> checkout ->
    uv sync, the updater's first touch lands after them) and every legacy
    pre-lease chain on the first rollout that ships this. Both read "cannot
    tell", never a stall — abandoning a host on this window is the too-eager
    verdict this rewrite exists to remove."""
    from datetime import UTC, datetime

    from ops import cluster_rpc as cr
    from shared.host_deploy_state import HostDeployState

    async def _reachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        return {}

    def _fake_read(machine=None, **_kw):
        return HostDeployState(
            machine=machine or "old",  # pyright: ignore[reportUnknownArgumentType]
            posture="paused",
            updated_at=datetime.now(UTC),
            updater_lease_expires_at=None,
        )

    monkeypatch.setattr(cr, "dispatch_to_machine", _reachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("cli.commands._update_phase_b.read", _fake_read)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("old", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"old": _cli.POLL_CONVERGING}


def test_poll_unreachable_host_is_never_a_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ops` is itself a service its own self-update stops, so silence is the expected
    reading through the middle of a healthy leg — the longest stretch of it on a
    Windows host. It must never become a verdict."""
    from ops import cluster_rpc as cr

    async def _unreachable(*, target_machine, kind, payload, timeout_s, ops_url=None, retries=None):
        raise cr.ClusterOpUnreachable("ops down mid-restart")

    monkeypatch.setattr(cr, "dispatch_to_machine", _unreachable)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_POLL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(_cli, "_POLL_INTERVAL_S", 0.01)

    out = _cli._poll_until_unpaused([("win", "http://unused")])
    assert {n: v.status for n, v in out.items()} == {"win": _cli.POLL_CONVERGING}


# ─── cmd_cluster_status (thin client over /api/cluster/roster) ────────────────


def _patch_roster_get(monkeypatch: pytest.MonkeyPatch, roster: list[dict]) -> list[str]:
    """Stub the gateway URL/headers + httpx.get so cmd_cluster_status renders `roster`."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    calls: list[str] = []

    def _fake_get(url, **_kw):
        calls.append(url)  # pyright: ignore[reportUnknownArgumentType]
        return _FakeResponse(roster)

    monkeypatch.setattr("httpx.get", _fake_get)  # pyright: ignore[reportUnknownArgumentType]
    return calls


def _machine_row(**overrides: object) -> dict[str, object]:
    """A real MachineStatus serialized to its wire dict, with field overrides.

    Building rows from the actual schema (not a hand-written dict) keeps the
    roster tests honest: if MachineStatus drops/renames a field the renderer
    reads, they fail here instead of drifting silently — which is exactly how the
    KeyError('role') crash shipped (the old fixtures carried a `role` field the
    wire schema no longer has).
    """
    from datetime import UTC, datetime

    from gateway.schemas import MachineStatus

    base = MachineStatus(
        name="test-host",
        serve_gateway=True,
        serve_agent_runner=True,
        gateway_url="http://gw:8000",
        up_since_at=datetime(2026, 6, 1, 7, 0, tzinfo=UTC),
        online=True,
        paused=False,
    )
    return base.model_copy(update=overrides).model_dump(mode="json")


def test_cmd_cluster_status_empty_roster_prints_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty roster from the gateway -> hint + exit 0."""
    calls = _patch_roster_get(monkeypatch, [])
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    assert calls == ["http://gw:8000/api/cluster/roster"]
    assert "machines table empty" in capsys.readouterr().out


def test_cmd_cluster_status_renders_online_stopped_offline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The roster's online / stopped_at fields render as online / stopped / offline."""
    from datetime import UTC, datetime

    roster = [
        _machine_row(name="test-host", online=True, stopped_at=None),
        _machine_row(
            name="wsl",
            online=False,
            paused=None,
            stopped_at=datetime(2026, 6, 1, 6, 0, tzinfo=UTC),
        ),
        _machine_row(name="corp", online=False, paused=None, stopped_at=None),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "test-host" in out and "online" in out
    assert "wsl" in out and "stopped" in out
    assert "corp" in out and "offline" in out


def test_cmd_cluster_status_renders_hold_column_and_banner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A live settle hold shows up twice: the banner naming the lease (the answer to
    "why was my deploy refused"), and `waited-on` on exactly the hosts the hold's note
    recorded — every other host reads `—`, which is "not named", not "converged"."""
    roster = [
        _machine_row(
            name="test-host",
            deploy_hold="machine-1:pid42 (held 5m, lease expires in 10m) — settling, waiting for: wsl",
            settle_waited_on=False,
        ),
        _machine_row(
            name="wsl",
            deploy_hold="machine-1:pid42 (held 5m, lease expires in 10m) — settling, waiting for: wsl",
            settle_waited_on=True,
        ),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        "deploy hold: machine-1:pid42 (held 5m, lease expires in 10m) — settling, waiting for: wsl"
    )
    # The banner states the operator-visible consequence, which is what brought them here.
    assert any("auto-rollback" in line for line in out[:5])
    header = next(line for line in out if line.startswith("name"))
    assert "hold" in header
    wsl_row = next(line for line in out if line.startswith("wsl"))
    assert "waited-on" in wsl_row
    local_row = next(line for line in out if line.startswith("test-host"))
    assert "waited-on" not in local_row


def _failed_update(**overrides: object) -> object:
    """A real LastUpdate, as the roster stamps it onto every row. Handed to
    `_machine_row` as the model so MachineStatus serializes it — a hand-built dict
    would validate against nothing and drift silently."""
    from datetime import UTC, datetime

    from shared.last_update import LastUpdate, UpdateOutcome

    base = LastUpdate(
        outcome=UpdateOutcome.INCOMPLETE,
        failed=True,
        target_sha="8bdd3667aa",
        origin="frontend",
        started_at=datetime(2026, 7, 30, 21, 10, tzinfo=UTC),
        failing_step="the gateway was not serving, so Phase B never fanned out",
    )
    return base.model_copy(update=overrides)


def test_cmd_cluster_status_states_a_failed_update_instead_of_leaving_a_sha_riddle(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The roster's `pin` / `code` cells are symptoms — a node that missed a rollout
    and a rollout that failed and rolled back produce the same mismatch. The banner
    states which, above everything else, with the step and whether the pin moved."""
    roster = [_machine_row(name="test-host", last_update=_failed_update())]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    out = capsys.readouterr().out.splitlines()
    assert "FAILED" in out[0] and "8bdd366" in out[0]
    top = "\n".join(out[:5])
    assert "Phase B never fanned out" in top
    assert "pin was left where it was" in top
    assert "next successful" in top.lower() or "next successful" in "\n".join(out[:6])


def test_cmd_cluster_status_shows_the_rollback_anchor_and_the_recovery_that_ran(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`last_known_good_sha` was recorded since the pin existed and shown nowhere, so
    a rollback presented as the pin moving backwards for no stated reason. With the
    anchor and the observer's own sentence, the pin change reads as the designed
    fallback it is."""
    roster = [
        _machine_row(
            name="test-host",
            cluster_last_known_good_sha="7e571b49aa",
            last_update=_failed_update(observed_by="rolled back 8bdd366 -> 7e571b4"),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    top = "\n".join(capsys.readouterr().out.splitlines()[:6])
    assert "since then: rolled back 8bdd366 -> 7e571b4" in top
    assert "rollback anchor (last known good): 7e571b4" in top


def test_cmd_cluster_status_dates_the_failure_so_a_stale_one_reads_as_stale(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Minutes old is a live incident; days old is a cluster nobody has updated
    since. The banner has to let those two be told apart at a glance."""
    from datetime import UTC, datetime, timedelta

    roster = [
        _machine_row(
            name="test-host",
            last_update=_failed_update(started_at=datetime.now(UTC) - timedelta(days=3)),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    assert "(3d ago)" in capsys.readouterr().out


def test_cmd_cluster_status_says_nothing_about_an_update_that_succeeded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silent on success by design: a permanent "last update: ok" line is one people
    stop reading, and this is the line that has to be read the once it appears."""
    from shared.last_update import UpdateOutcome

    roster = [
        _machine_row(
            name="test-host",
            last_update=_failed_update(outcome=UpdateOutcome.CLEAN, failed=False),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("name"), "a successful update must add no banner at all"


def test_cmd_cluster_status_marks_a_recovered_update_as_a_warning_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A recovery still has to be stated — that silence is the 2026-07-30 bug — but
    it is not the same call to action as an unhandled failure, so it does not get the
    same glyph. Giving both `✗` is how the actionable one stops being read."""
    from shared.last_update import UpdateOutcome

    roster = [
        _machine_row(
            name="test-host",
            last_update=_failed_update(
                outcome=UpdateOutcome.RECOVERED,
                observed_by="rolled back 8bdd366 -> 7e571b4",
            ),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("⚠"), f"a recovered update must not read as a live failure: {out[0]}"
    assert "RECOVERED" in out[0]


def test_cmd_cluster_status_names_the_rollouts_own_log_when_the_record_has_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The record carries the log the run was actually writing, so the banner points
    at that file instead of at the glob an operator then has to pick from by mtime."""
    roster = [
        _machine_row(
            name="test-host",
            last_update=_failed_update(log_path="/home/ava/.ava/logs/rollout-1785470000.log"),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    top = "\n".join(capsys.readouterr().out.splitlines()[:6])
    assert "/home/ava/.ava/logs/rollout-1785470000.log" in top
    assert "rollout-<epoch>.log" not in top


def test_cmd_cluster_status_falls_back_to_the_log_pattern_when_none_was_recorded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A foreground `ava cluster update --local` has no log of its own. Naming a specific
    file there would be a guess, so the banner describes where rollout logs live."""
    roster = [_machine_row(name="test-host", last_update=_failed_update(log_path=None))]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    assert "rollout-<epoch>.log" in capsys.readouterr().out


def test_cmd_cluster_status_reports_an_orphaned_update_as_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reading no process could have filed for itself: the orchestration died,
    so the record was never closed and the lease it held is gone."""
    from shared.last_update import UpdateOutcome

    roster = [
        _machine_row(
            name="test-host",
            last_update=_failed_update(outcome=UpdateOutcome.ORPHANED, failing_step=None),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    assert "died without reporting an outcome" in capsys.readouterr().out


def test_cmd_cluster_status_prints_no_banner_when_no_hold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No live lease -> no banner at all. A blank `hold` column is not evidence the
    cluster is free (a host-local watchdog update takes no lease), so the roster does
    not claim it is."""
    _patch_roster_get(monkeypatch, [_machine_row(name="test-host")])
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "deploy hold" not in out
    assert out.splitlines()[0].startswith("name")


def test_cmd_cluster_status_fails_fast_on_http_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 5xx from the gateway exits 1 with the status named — fail-fast, no
    silent fallback, and no unhandled traceback (#219)."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.get", lambda *_a, **_kw: _FakeResponse([], status_code=503))  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 503" in err


def test_cmd_cluster_restart_posts_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava cluster restart` POSTs /api/cluster/restart and reports the updater session."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    calls: list[str] = []

    def _fake_post(url, **_kw):
        calls.append(url)  # pyright: ignore[reportUnknownArgumentType]
        return _FakeResponse({"session": "ava-updater", "log": "/var/log/u.log"})

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_cluster_restart()
    assert rc == 0
    assert calls == ["http://gw:8000/api/cluster/restart"]
    assert "ava-updater" in capsys.readouterr().out


def test_cmd_update_gateway_default_posts_rollout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gateway-capable host's default is the SAME POST every host sends —
    no local spawn branch (user ruling 2026-08-21, issue #216). The gateway
    answers by starting the detached rollout; the CLI just prints the
    session/log it is told about."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))

    def _no_local(*_a, **_kw):
        raise AssertionError(
            "foreground `ava cluster update` must not run the in-process orchestration"
        )

    monkeypatch.setattr(_cli, "_run_gateway_orchestration", _no_local)  # pyright: ignore[reportUnknownArgumentType]
    from typing import cast

    calls: list[tuple[str, dict[str, object]]] = []

    class _Resp:
        status_code = 202

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"session": "ava-rollout", "log": "/var/log/u.log"}

    def _fake_post(url: str, **_kw: object) -> _Resp:
        calls.append((url, cast(dict[str, object], _kw.get("json"))))
        return _Resp()

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_update()
    assert rc == 0
    # a human-invoked `ava cluster update` self-identifies as cli:<machine>
    assert calls[0][0] == "http://gw:8000/api/cluster/rollout"
    assert cast(str, calls[0][1]["origin"]).startswith("cli:")
    assert "ava-rollout" in capsys.readouterr().out


def test_cmd_update_local_forces_in_process_orchestration(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava cluster update --local` (what the detached rollout session runs) forces the
    in-process gateway orchestration and never POSTs."""
    from cli.commands import update as _up_mod

    monkeypatch.setattr(_up_mod, "_repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(_up_mod, "ava_home", lambda: Path("/home"))
    monkeypatch.setattr(_up_mod, "get_record", lambda _home: None)  # pyright: ignore[reportUnknownArgumentType]

    def _no_post(*_a: object, **_kw: object) -> None:
        raise AssertionError("--local must not POST the gateway")

    monkeypatch.setattr("httpx.post", _no_post)  # pyright: ignore[reportUnknownArgumentType]
    ran: list[bool] = []
    monkeypatch.setattr(
        _cli,
        "_run_gateway_orchestration",
        lambda *_a, **_kw: ran.append(True) or 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    rc = _cli.cmd_update(local=True)
    assert rc == 0
    assert ran == [True]


def test_cmd_update_rollout_conflict_and_noop_are_friendly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rollout endpoint's ordinary refusals — 409 update-in-flight and 422
    nothing-to-update — print one stderr line and exit 1 / 0 respectively,
    not a raw traceback (the deploy-window case a second operator most often
    hits)."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    class _Resp:
        def __init__(self, status_code: int, detail: str):
            self.status_code = status_code
            self._detail = detail

        def raise_for_status(self) -> None:
            import httpx

            if self.status_code >= 400:
                request = httpx.Request("POST", "http://gw:8000")
                response = httpx.Response(self.status_code, request=request)
                raise httpx.HTTPStatusError(self._detail, request=request, response=response)

        def json(self) -> dict[str, object]:
            return {"detail": self._detail}

    monkeypatch.setattr(
        "httpx.post",
        lambda *_a, **_kw: _Resp(409, "deploy already in flight"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _cli.cmd_update() == 1
    assert "deploy already in flight" in capsys.readouterr().err

    monkeypatch.setattr(
        "httpx.post",
        lambda *_a, **_kw: _Resp(422, "already up to date"),  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _cli.cmd_update() == 0  # a no-op update is not a failure
    assert "already up to date" in capsys.readouterr().err


# ─── gateway-backed CLI paths (stop announce / status snapshot) ──────────────


class _FakeResponse:
    def __init__(self, payload: dict | list, status_code: int = 200):
        self._payload = payload  # pyright: ignore[reportUnknownMemberType]
        self.status_code = status_code

    def raise_for_status(self) -> None:
        import httpx

        if self.status_code >= 400:
            request = httpx.Request("GET", "http://gw:8000")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(f"{self.status_code}", request=request, response=response)

    def json(self) -> dict | list:
        return self._payload  # pyright: ignore[reportUnknownMemberType]


def _patch_gateway_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub gateway URL/headers resolution so the HTTP helpers don't hit settings."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")


def _patch_stop_teardown(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Run the real `_do_stop` with its side effects stubbed: no live sessions
    to kill, gateway-role host, data-plane teardown recorded into `events`."""
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway", "agent-runner"}))
    monkeypatch.setattr("cli.commands.stop._repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(
        "cli.commands._cluster_instance.stop_cluster_instance",
        lambda: events.append("infra") or 0,
    )


def test_cmd_stop_announces_stopping_after_confirm_before_teardown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A confirmed `ava stop` best-effort POSTs
    /api/cluster/stopping?machine=<self>&home=<self-home> before the local
    teardown (so the cluster view shows 'stopped', not 'offline'). `home`
    identifies THIS unit so a co-located peer keeps its caps."""
    from shared.paths import ava_home

    _patch_gateway_http(monkeypatch)
    monkeypatch.setattr("shared.machine.machine_name", lambda: "test-host")
    events: list[str] = []
    calls: list[tuple[str, dict]] = []

    def _fake_post(url, **kwargs):
        events.append("announce")
        calls.append((url, kwargs))  # pyright: ignore[reportUnknownMemberType]
        return _FakeResponse({"machine": "test-host"})

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    _patch_stop_teardown(monkeypatch, events)

    rc = _cli.cmd_stop(require_confirmation=False, force=True)
    assert rc == 0
    assert calls[0][0] == "http://gw:8000/api/cluster/stopping"
    assert calls[0][1]["params"] == {"machine": "test-host", "home": str(ava_home())}
    assert events == ["announce", "infra"]  # announced first, then teardown ran


def test_cmd_stop_aborted_confirm_does_not_announce(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Declining the confirm prompt must leave the roster untouched: the stopping
    announce stamps `machines.stopped_at`, and only the next `ava start` clears
    it — an announce fired before the gate would mark a running host 'stopped'."""
    _patch_gateway_http(monkeypatch)
    monkeypatch.setattr("shared.machine.machine_name", lambda: "test-host")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")  # pyright: ignore[reportUnknownArgumentType]
    events: list[str] = []
    monkeypatch.setattr("httpx.post", lambda *_a, **_kw: events.append("announce"))  # pyright: ignore[reportUnknownArgumentType]
    _patch_stop_teardown(monkeypatch, events)

    rc = _cli.cmd_stop(force=True)
    assert rc == 0
    assert events == []  # no announce, no teardown
    assert "aborted" in capsys.readouterr().out


def test_cmd_stop_proceeds_when_announce_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the stopping announce can't reach the gateway, `ava stop` logs and still
    tears down — the announce is best-effort, never a blocker."""
    _patch_gateway_http(monkeypatch)
    monkeypatch.setattr("shared.machine.machine_name", lambda: "wsl")

    def _boom(*_a, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("httpx.post", _boom)  # pyright: ignore[reportUnknownArgumentType]
    events: list[str] = []
    _patch_stop_teardown(monkeypatch, events)

    rc = _cli.cmd_stop(require_confirmation=False, force=True)
    assert rc == 0
    assert events == ["infra"]  # teardown still ran
    assert "could not announce shutdown" in capsys.readouterr().out


def test_cmd_status_shows_gateway_snapshot_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava status` (no flag) runs local probes AND prints the gateway snapshot."""
    _patch_gateway_http(monkeypatch)
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli.subprocess, "run", lambda *_a, **_kw: _FakeResult(returncode=0))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "httpx.get",
        lambda *_a, **_kw: _FakeResponse(  # pyright: ignore[reportUnknownArgumentType]
            {
                "machine_name": "test-host",
                "serve_gateway": True,
                "serve_agent_runner": False,
                "paused": False,
            }
        ),
    )
    rc = _cli.cmd_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert _sess("gateway") in out  # local probe section still present
    assert "gateway cluster status" in out  # gateway supplement section
    assert "test-host" in out


def test_cmd_status_gateway_unreachable_is_inline_not_fatal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A down gateway prints inline; `ava status` still returns 0 (local probes ran)."""
    import httpx

    _patch_gateway_http(monkeypatch)
    monkeypatch.setattr(_cli, "_has_session", lambda _s: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_curl_ok", lambda _u: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli.subprocess, "run", lambda *_a, **_kw: _FakeResult(returncode=0))  # pyright: ignore[reportUnknownArgumentType]

    def _boom(*_a, **_kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("httpx.get", _boom)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_status()
    assert rc == 0
    assert "gateway unreachable" in capsys.readouterr().out


# ─── `ava status` keeps a live host reading with no observability backend ─────


def test_status_prints_a_live_host_reading(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """`ava status` reads CPU/memory/disk straight from psutil.

    Since issue #46 the host HISTORY is Prometheus's; this line is the answer
    that must survive a deployment whose LGTM backend is down or was never
    deployed, so it must not go through the observability stack at all.
    """
    from cli.commands import status as status_mod

    monkeypatch.setattr(status_mod, "_repo_root", lambda: "/repo")
    monkeypatch.setattr(status_mod, "_cluster_pin_status", lambda: ("aaaaaaa", "aaaaaaa"))
    monkeypatch.setattr(status_mod, "prod_source_pin_relation", lambda _p, _h: "aligned")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(status_mod, "_detect_prod_source_drift", lambda: None)
    monkeypatch.setattr(status_mod, "_print_gateway_cluster_status", lambda: None)
    monkeypatch.setattr(status_mod, "print_data_plane_status", lambda: None)
    monkeypatch.setattr(status_mod, "_print_service_row", lambda *_a: None)  # pyright: ignore[reportUnknownArgumentType]

    assert status_mod.cmd_status() == 0
    out = cast(str, capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    assert "host (live cpu/memory/disk):" in out
    assert re.search(r"cpu \d+%\s+memory \d+% \([\d.]+/[\d.]+ GB\)\s+disk \d+%", out)


def test_status_host_reading_failure_does_not_hide_the_rest(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """A host without psutil still gets the service table and the pin section —
    the reading degrades to its own reason line, it does not abort the verb."""
    from cli.commands import status as status_mod

    monkeypatch.setattr(status_mod, "_repo_root", lambda: "/repo")
    monkeypatch.setattr(status_mod, "_cluster_pin_status", lambda: ("aaaaaaa", "aaaaaaa"))
    monkeypatch.setattr(status_mod, "prod_source_pin_relation", lambda _p, _h: "aligned")  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(status_mod, "_detect_prod_source_drift", lambda: None)
    monkeypatch.setattr(status_mod, "_print_gateway_cluster_status", lambda: None)
    monkeypatch.setattr(status_mod, "print_data_plane_status", lambda: None)
    monkeypatch.setattr(status_mod, "_print_service_row", lambda *_a: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        "shared.resource_sample.resource_sample",
        lambda: (_ for _ in ()).throw(RuntimeError("no psutil here")),
    )

    assert status_mod.cmd_status() == 0
    out = cast(str, capsys.readouterr().out)  # pyright: ignore[reportUnknownMemberType]
    assert "unavailable (no psutil here)" in out
    assert "[ava status]" in out


def test_retired_service_failure_prevents_start_converge_and_migrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli.commands import start

    retired = MagicMock(side_effect=TimeoutError("retired service is still running"))
    converge, migrate = MagicMock(), MagicMock()
    monkeypatch.setattr("cli.commands._retired_services.stop_retired_services", retired)
    monkeypatch.setattr(_cli, "converge_host", converge)
    monkeypatch.setattr(start, "cmd_migrations_apply", migrate)
    assert _cli.cmd_start() == 1
    retired.assert_called_once()
    converge.assert_not_called()
    migrate.assert_not_called()
