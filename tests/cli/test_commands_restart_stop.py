"""Restart, stop, and stop announcement commands; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cli import commands as _cli
from cli.commands.stop import _force_stop
from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import (
    _FakeResponse,
    _FakeResult,
    _FakeSessionBackend,
    _git_aware,
    _patch_gateway_http,
    _sess,
)
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base
from tests.cli._commands_helpers import _noop_start_prechecks as _noop_start_prechecks

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
    monkeypatch.setattr(_cli, "_preflight_start_readiness", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]
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
    monkeypatch.setattr(_cli, "_preflight_start_readiness", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]
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
    monkeypatch.setattr(_cli, "_preflight_start_readiness", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_do_stop", lambda *_args, **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_cmd_start_body", lambda **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]

    assert _cli.cmd_restart() == 0
    assert seen[0] == "restart"


def test_cmd_restart_finishes_the_journal_only_when_it_owns_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outer operation's still-running journal is never closed by the nested
    restart — the owns_journal guard _temporary_stop keeps (task #2898)."""
    from shared import lifecycle_status

    monkeypatch.setattr(_cli, "_preflight_probes", lambda: 0)
    monkeypatch.setattr(_cli, "_preflight_start_readiness", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_do_stop", lambda *_args, **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_cmd_start_body", lambda **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]
    finished: list[int] = []
    monkeypatch.setattr(
        lifecycle_status,
        "finish",
        lambda rc, **_kwargs: finished.append(rc),  # pyright: ignore[reportUnknownArgumentType]
    )

    monkeypatch.setattr(
        lifecycle_status,
        "begin",
        lambda _operation, **_kwargs: False,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _cli.cmd_restart() == 0
    assert finished == []  # the outer operation still owns the journal

    monkeypatch.setattr(
        lifecycle_status,
        "begin",
        lambda _operation, **_kwargs: True,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _cli.cmd_restart() == 0
    assert finished == [0]


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


def test_cmd_restart_aborts_when_start_readiness_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task #3165: a start-readiness refusal (private-tree roots, ports, venv
    entry points, migrations) stops nothing — the same decline contract as the
    probes gate, with stop and start neither run. The gate is called with
    `check_launcher=False`: this start is in-process and never execs
    `.venv/bin/ava`."""
    from shared.exit_codes import RESTART_DECLINED_EXIT_CODE

    stopped: list[bool] = []
    start_called: list[bool] = []
    gate_calls: list[dict[str, bool]] = []

    def _gate(_repo, **kwargs: bool) -> int:
        gate_calls.append(kwargs)
        return 1

    monkeypatch.setattr(_cli, "_preflight_probes", lambda: 0)
    monkeypatch.setattr(_cli, "_preflight_start_readiness", _gate)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_do_stop", lambda *_a, **_kw: stopped.append(True) or 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_cmd_start_body", lambda **_kw: start_called.append(True) or 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_release_self_heal_pause", lambda: None)

    rc = _cli.cmd_restart()

    assert rc == RESTART_DECLINED_EXIT_CODE, "a readiness refusal must decline, not fail"
    assert stopped == [], "must not stop services when the readiness gate refuses"
    assert start_called == [], "must not start after a declined restart"
    assert gate_calls == [{"check_launcher": False}], (
        "the restart caller must skip the ava-launcher check (in-process start)"
    )


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


# ─── gateway-backed CLI paths (stop announce) ──────────────────────────────
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
