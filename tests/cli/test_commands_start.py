"""Session helpers, service readiness, and start commands; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli import commands as _cli
from cli.commands import _collect_setup_values as _real_collect_setup_values
from shared.config import settings
from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import (
    _FakeResult,
    _FakeSessionBackend,
    _git_aware,
    _real_wait_for_services_ready,
    _sess,
    _spec,
)
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base
from tests.cli._commands_helpers import _noop_start_prechecks as _noop_start_prechecks

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
