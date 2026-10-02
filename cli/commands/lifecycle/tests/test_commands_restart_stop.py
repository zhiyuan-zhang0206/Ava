"""Restart, stop, and stop announcement commands; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import subprocess as subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cli.commands._probe as _probe_commands
import cli.commands._repo as _repo_commands
import cli.commands._setup as _setup_commands
import cli.commands.converge.host as converge_host
import cli.commands.lifecycle._start_readiness_preflight as _start_readiness_preflight_commands
import cli.commands.lifecycle.root_driver as _root_driver_commands
import cli.commands.lifecycle.start as _start_commands
import cli.commands.lifecycle.stop as _stop_commands
from base.deploy.lifecycle.start_serving import RootBirth
from cli.commands.lifecycle.stop import _force_stop
from cli.tests._commands_helpers import (
    _FakeResponse,
    _FakeResult,
    _git_aware,
    _patch_gateway_http,
)
from cli.tests._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base

_real_reap_cluster_chrome = _stop_commands._reap_cluster_chrome


@pytest.fixture(autouse=True)
def _noop_start_prechecks(serving_root: RootBirth, monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_start's multi-machine setup collection + converge_host + register_self
    are all noop in an importing module — here we test session / docker / stop / status call shapes,
    orthogonal to setup. Setup behavior itself is left to base/test_machine.py + the setup-ergonomics tests in `test_commands_start.py`.

    Default role="gateway" (full service set). To test secondary, explicitly override:
        monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"agent-runner"}))
        monkeypatch.setattr(_cli, "_collect_setup_values", lambda: (..., []))"""
    from base.deploy.release.runtime_interpreter import LoadedRuntimeIdentity

    def fixture_runtime(_self: object) -> LoadedRuntimeIdentity:
        return serving_root.runtime

    monkeypatch.setattr("cli.start_runtime.StartRuntime.identity", fixture_runtime)

    def _fake_collect() -> tuple[dict[str, str], list]:
        return {
            "machine_name": "test-machine",
            "machine_role": "gateway",
            "memory_remote": "git@github.com:test/AvaMemory.git",
            "gateway_url": "http://test-gateway:8000",
        }, []

    monkeypatch.setattr(_setup_commands, "_collect_setup_values", _fake_collect)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_root_driver_commands, "admit_live_start", lambda *_a, **_kw: False)  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    monkeypatch.setattr(converge_host, "converge_host", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    # The per-cluster pg/redis bring-up (`_ensure_gateway_data_plane`) starts a real
    # native instance under $AVA_HOME. These tests assert session/stop/status call
    # shapes, not infra, so stub it to a noop — keeping them hermetic regardless of
    # the dev host's pg/redis.
    from cli.commands.lifecycle import start as _start_mod

    monkeypatch.setattr(_start_mod, "_ensure_gateway_data_plane", lambda: 0)
    monkeypatch.setattr("cli.commands.data_plane.bringup.prepare_gateway_schema", lambda: None)
    monkeypatch.setattr(
        "cli.commands.data_plane.bringup.complete_gateway_data_plane",
        lambda **_kw: None,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    from cli.commands.lifecycle.root_driver import LaunchOutcome

    monkeypatch.setattr(
        _root_driver_commands,
        "_launch_service_tree",
        lambda roster, *_a, **_kw: LaunchOutcome(roster, ()),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    monkeypatch.setattr(
        _root_driver_commands,
        "wait_for_service_tree",
        lambda *_a, **_kw: _probe_commands.ReadinessWait((), 0.0, sessions_gone=False),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )

    # _roles_or_none (stop/status/converge) + machine_role (cmd_start service
    # resolution) both read the AVA_MACHINE_SERVE_* settings; the test env sets only
    # the agent-runner flag. Pin both to gateway so the default path is the
    # full-service gateway box, deterministic regardless of the dev host's
    # environment. Agent-runner tests override machine_role explicitly.
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("base.cluster.machine.machine_role", lambda: frozenset({"gateway"}))
    # register_self goes to central DB UPSERT; test does not need real writes. cmd_start goes
    # through _register_machine_or_die which internally imports register_self, directly patch the helper to return 0.
    monkeypatch.setattr(_repo_commands, "_register_machine_or_die", lambda _db, _resolved, _role: 0)  # pyright: ignore[reportUnknownArgumentType]
    # secondary path will run _probe_gateway_or_die; primary does not call it, adding here
    # ensures secondary tests can also reuse the default noop.
    monkeypatch.setattr(_repo_commands, "_probe_gateway_or_die", lambda _url: 0)  # pyright: ignore[reportUnknownArgumentType]
    # _assert_schema_current_or_die truly calls DB; tests don't need real schema query, directly patch.
    monkeypatch.setattr(_repo_commands, "_assert_schema_current_or_die", lambda: 0)
    # Root service preparation must not install frontend dependencies in unit tests.
    from cli.commands import _repo
    from cli.commands.lifecycle import root_driver

    monkeypatch.setattr(_repo, "_ensure_frontend_deps", lambda _repo: None)  # pyright: ignore[reportUnknownArgumentType]

    # These call-shape tests use the suite's owner DB URL, not an enrolled
    # runner's bootstrap projection. Credential forwarding has its own tests
    # in test_agent_profile_launch_env.py.
    def _fixture_runner_url() -> str:
        return "postgresql://ava_runner:test-runner@127.0.0.1:1/ava_citest"

    monkeypatch.setattr(root_driver, "runner_db_url_projection", _fixture_runner_url)


@pytest.fixture(autouse=True)
def _root_stop_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """No command-layer test contacts or signals a native application root."""
    monkeypatch.setattr(_root_driver_commands, "_root_tree_plan", lambda _preserve: [])  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    monkeypatch.setattr(_root_driver_commands, "_stop_root_service_tree", lambda **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    monkeypatch.setattr(_stop_commands, "_reap_cluster_chrome", lambda: None)
    monkeypatch.setattr("cli.commands.lifecycle.stop._stop_terminals_force", lambda: None)


# ─── restart ─────────────────────────────────────────────────────────────────


def test_cmd_restart_succeeds_non_interactively(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_restart succeeds in a non-interactive context (the detached-updater path) —
    like cmd_start, it needs no tty."""
    import sys as _sys

    monkeypatch.setattr(_sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        subprocess,
        "run",
        _git_aware(lambda *_a, **_kw: _FakeResult(returncode=0)),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_stop_commands, "_do_stop", MagicMock(return_value=0))
    monkeypatch.setattr("cli.commands.lifecycle.start.cmd_migrations_apply", list[str])
    monkeypatch.setattr(
        _start_readiness_preflight_commands,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    rc = _stop_commands.cmd_restart()
    assert rc == 0


def test_cmd_restart_calls_stop_then_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_restart invokes stop (require_confirmation=False) then _cmd_start_body in order."""

    order: list[str] = []

    def fake_do_stop(_repo, *, require_confirmation, keep_infra=False, force=False) -> int:
        assert require_confirmation is False, "cmd_restart must skip stdin confirmation"
        assert force is False
        assert keep_infra is True, (
            "an internal restart must keep the shared pg/redis up — stopping the "
            "data plane kills the orchestrator's DB polling mid-rollout"
        )
        order.append("stop")
        return 0

    def fake_cmd_start_body(**kwargs: object) -> int:
        from cli.start_runtime import StartRuntime

        assert kwargs["persist_services"] is False
        assert isinstance(kwargs["runtime"], StartRuntime)
        order.append("start")
        return 0

    monkeypatch.setattr(_stop_commands, "_do_stop", fake_do_stop)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_start_commands, "_cmd_start_body", fake_cmd_start_body)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_readiness_preflight_commands,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    rc = _stop_commands.cmd_restart()
    assert order == ["stop", "start"]
    assert rc == 0


def test_cmd_restart_finishes_the_journal_only_when_it_owns_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outer operation's still-running journal is never closed by the nested
    restart — the owns_journal guard _temporary_stop keeps (task #2898)."""
    from base.deploy.lifecycle import status_journal

    monkeypatch.setattr(_repo_commands, "_preflight_probes", lambda _db: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_readiness_preflight_commands,
        "preflight_start_readiness",
        lambda *_a, **_k: 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_args, **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_start_commands, "_cmd_start_body", lambda **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]
    finished: list[int] = []
    monkeypatch.setattr(
        status_journal,
        "finish",
        lambda rc, **_kwargs: finished.append(rc),  # pyright: ignore[reportUnknownArgumentType]
    )

    monkeypatch.setattr(
        status_journal,
        "begin",
        lambda _operation, **_kwargs: False,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _stop_commands.cmd_restart() == 0
    assert finished == []  # the outer operation still owns the journal

    monkeypatch.setattr(
        status_journal,
        "begin",
        lambda _operation, **_kwargs: True,  # pyright: ignore[reportUnknownArgumentType]
    )
    assert _stop_commands.cmd_restart() == 0
    assert finished == [0]


def test_cmd_restart_short_circuits_on_stop_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If stop returns non-zero, cmd_restart propagates without calling start."""

    start_called: list[bool] = []

    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_a, **_kw: 1)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_commands,
        "_cmd_start_body",
        lambda **_kw: start_called.append(True) or 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    rc = _stop_commands.cmd_restart()
    assert rc == 1
    assert start_called == [], "start must not run when stop fails"


def test_cmd_restart_aborts_when_preflight_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """When preflight probes fail, cmd_restart aborts without stopping — and says so
    with its OWN exit code, since "nothing was stopped, host still serving" is what
    the detached updater must not run `ava start` over."""
    from base.agents.exit_codes import RESTART_DECLINED_EXIT_CODE

    stopped: list[bool] = []
    start_called: list[bool] = []

    monkeypatch.setattr(
        _repo_commands,
        "_preflight_probes",
        lambda _db: 1,  # pyright: ignore[reportUnknownArgumentType]
    )  # simulate failure  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_a, **_kw: stopped.append(True) or 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_commands,
        "_cmd_start_body",
        lambda **_kw: start_called.append(True) or 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "_release_self_heal_pause", lambda: None)

    rc = _stop_commands.cmd_restart()
    assert rc == RESTART_DECLINED_EXIT_CODE, "preflight failure must propagate non-zero"
    assert stopped == [], "must not stop services when preflight fails"
    assert start_called == [], "must not start when preflight fails"


def test_cmd_restart_aborts_when_start_readiness_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task #3165: a start-readiness refusal (private-tree roots, ports, venv
    entry points, migrations) stops nothing — the same decline contract as the
    probes gate, with stop and start neither run. The gate is called with
    `check_launcher=False`: this start is in-process and never execs
    `.venv/bin/ava`."""
    from base.agents.exit_codes import RESTART_DECLINED_EXIT_CODE

    stopped: list[bool] = []
    start_called: list[bool] = []
    gate_calls: list[dict[str, object]] = []

    def _gate(_repo: Path, **kwargs: object) -> int:
        gate_calls.append(kwargs)
        return 1

    monkeypatch.setattr(_repo_commands, "_preflight_probes", lambda _db: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_start_readiness_preflight_commands, "preflight_start_readiness", _gate)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "_do_stop", lambda *_a, **_kw: stopped.append(True) or 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _start_commands,
        "_cmd_start_body",
        lambda **_kw: start_called.append(True) or 0,  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_stop_commands, "_release_self_heal_pause", lambda: None)

    rc = _stop_commands.cmd_restart()

    assert rc == RESTART_DECLINED_EXIT_CODE, "a readiness refusal must decline, not fail"
    assert stopped == [], "must not stop services when the readiness gate refuses"
    assert start_called == [], "must not start after a declined restart"
    from cli.start_runtime import StartRuntime

    assert len(gate_calls) == 1
    assert gate_calls[0]["check_launcher"] is False
    assert isinstance(gate_calls[0]["runtime"], StartRuntime)


# ─── stop (stdin confirmation) ────────────────────────────────────────────────────────


def test_stop_aborts_on_no(monkeypatch: pytest.MonkeyPatch) -> None:
    """stdin input not y → abort, no kill / down commands called."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")  # pyright: ignore[reportUnknownArgumentType]

    def fake_run(args, **_kwargs):
        raise AssertionError(f"subprocess.run should not be called: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    rc = _stop_commands.cmd_stop(force=True)
    assert rc == 0


def test_stop_proceeds_on_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confirmed force stop asks root before stopping the private data plane."""
    events: list[str] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    monkeypatch.setattr(
        _root_driver_commands,
        "_stop_root_service_tree",
        lambda **_kw: events.append("root"),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    monkeypatch.setattr(
        "cli.commands.data_plane.cluster_instance.stop_cluster_instance",
        lambda: events.append("infra") or 0,
    )
    assert _stop_commands.cmd_stop(force=True) == 0
    assert events == ["root", "infra"]


def test_stop_revokes_serving_before_stopping_root(
    serving_root: RootBirth, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from base.deploy.lifecycle import start_serving

    monkeypatch.setattr(start_serving, "state_path", lambda: tmp_path / "start-serving.json")
    generation = start_serving.begin_start()
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime) is True
    observed: list[bool] = []
    monkeypatch.setattr(
        _root_driver_commands,
        "_stop_root_service_tree",
        lambda **_kw: observed.append(start_serving.is_serving()),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    monkeypatch.setattr("cli.commands.data_plane.cluster_instance.stop_cluster_instance", lambda: 0)
    assert _force_stop(tmp_path, require_confirmation=False) == 0
    assert observed == [False]


def test_do_stop_keep_infra_skips_infra_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        _root_driver_commands,
        "_stop_root_service_tree",
        lambda **_kw: events.append("root"),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    monkeypatch.setattr(
        "cli.commands.data_plane.cluster_instance.stop_cluster_instance",
        lambda: events.append("infra") or 0,
    )
    assert _force_stop(tmp_path, require_confirmation=False, keep_infra=True) == 0
    assert events == ["root"]


def test_do_stop_keeps_browser_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []
    reaps: list[int] = []
    monkeypatch.setattr(
        _root_driver_commands,
        "_stop_root_service_tree",
        lambda **kw: calls.append(kw),  # pyright: ignore[reportUnknownArgumentType] — untyped test double
    )
    monkeypatch.setattr(_stop_commands, "_reap_cluster_chrome", lambda: reaps.append(1))
    monkeypatch.setattr("cli.commands.data_plane.cluster_instance.stop_cluster_instance", lambda: 0)
    assert _force_stop(tmp_path, require_confirmation=False) == 0
    assert calls == [{"preserve": frozenset({"browser"}), "force": True}]
    assert reaps == []


def test_do_stop_stop_browser_kills_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events: list[str] = []

    def stop_root(**kwargs: object) -> None:
        assert kwargs == {"preserve": frozenset(), "force": True}
        events.append("root")

    monkeypatch.setattr(_root_driver_commands, "_stop_root_service_tree", stop_root)
    monkeypatch.setattr(_stop_commands, "_reap_cluster_chrome", lambda: events.append("browser"))
    monkeypatch.setattr(
        "cli.commands.data_plane.cluster_instance.stop_cluster_instance",
        lambda: events.append("infra") or 0,
    )
    assert _force_stop(tmp_path, require_confirmation=False, keep_browser=False) == 0
    assert events == ["root", "browser", "infra"]


def test_reap_cluster_chrome_reports_pids_and_survives_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
) -> None:
    """The CLI seam: report what was reaped, stay silent when there was nothing,
    and never let a sweep failure fail the teardown around it."""

    monkeypatch.setattr("services.browser.orphan.reap_cluster_chrome", lambda: [4242])
    _real_reap_cluster_chrome()
    assert "4242" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]

    monkeypatch.setattr("services.browser.orphan.reap_cluster_chrome", list)
    _real_reap_cluster_chrome()
    assert capsys.readouterr().out == "", "nothing to reap prints nothing"  # pyright: ignore[reportUnknownMemberType]

    def _boom() -> list[int]:
        raise RuntimeError("process table unavailable")

    monkeypatch.setattr("services.browser.orphan.reap_cluster_chrome", _boom)
    _real_reap_cluster_chrome()  # must not raise
    assert "could not sweep" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_cmd_stop_stop_browser_flag_threads_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`cmd_stop(stop_browser=...)` maps to `_do_stop(keep_browser=not stop_browser)`."""
    from cli.commands.lifecycle import stop as _stop_mod

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


# ─── gateway-backed CLI paths (stop announce) ──────────────────────────────
def _patch_stop_teardown(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> None:
    """Run force stop with a private root boundary and recorded storage teardown."""
    monkeypatch.setattr(
        _repo_commands, "_roles_or_none", lambda: frozenset({"gateway", "agent-runner"})
    )
    monkeypatch.setattr("cli.commands.lifecycle.stop._repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(
        "cli.commands.data_plane.cluster_instance.stop_cluster_instance",
        lambda: events.append("infra") or 0,
    )


def test_cmd_stop_announces_stopping_after_confirm_before_teardown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A confirmed `ava stop` best-effort POSTs
    /api/cluster/stopping?machine=<self>&home=<self-home> before the local
    teardown (so the cluster view shows 'stopped', not 'offline'). `home`
    identifies THIS unit so a co-located peer keeps its caps."""
    from base.paths import ava_home

    _patch_gateway_http(monkeypatch)
    monkeypatch.setattr("base.cluster.machine.machine_name", lambda: "test-host")
    events: list[str] = []
    calls: list[tuple[str, dict]] = []

    def _fake_post(url, **kwargs):
        events.append("announce")
        calls.append((url, kwargs))  # pyright: ignore[reportUnknownMemberType]
        return _FakeResponse({"machine": "test-host"})

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    _patch_stop_teardown(monkeypatch, events)

    rc = _stop_commands.cmd_stop(require_confirmation=False, force=True)
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
    monkeypatch.setattr("base.cluster.machine.machine_name", lambda: "test-host")
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")  # pyright: ignore[reportUnknownArgumentType]
    events: list[str] = []
    monkeypatch.setattr("httpx.post", lambda *_a, **_kw: events.append("announce"))  # pyright: ignore[reportUnknownArgumentType]
    _patch_stop_teardown(monkeypatch, events)

    rc = _stop_commands.cmd_stop(force=True)
    assert rc == 0
    assert events == []  # no announce, no teardown
    assert "aborted" in capsys.readouterr().out


def test_cmd_stop_proceeds_when_announce_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the stopping announce can't reach the gateway, `ava stop` logs and still
    tears down — the announce is best-effort, never a blocker."""
    _patch_gateway_http(monkeypatch)
    monkeypatch.setattr("base.cluster.machine.machine_name", lambda: "wsl")

    def _boom(*_a, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("httpx.post", _boom)  # pyright: ignore[reportUnknownArgumentType]
    events: list[str] = []
    _patch_stop_teardown(monkeypatch, events)

    rc = _stop_commands.cmd_stop(require_confirmation=False, force=True)
    assert rc == 0
    assert events == ["infra"]  # teardown still ran
    assert "could not announce shutdown" in capsys.readouterr().out
