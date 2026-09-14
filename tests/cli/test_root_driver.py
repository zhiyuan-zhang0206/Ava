"""The root-driven start/stop path (W1.2e-2).

The switch is off by default and the session path must stay byte-identical
when it is; when it is on, start/stop drive the ava-root supervisor instead of
named sessions. These tests cover the fork, the ensure/reconcile semantics,
the readiness contract on the root's status surface, and the stop mapping —
all against fakes: nothing here launches a real daemon.

The conftest readiness guard stubs both waits at the `cli.commands` namespace;
the unit tests below call the root wait directly (not through the guard's
name), and the integration tests re-pin the names they assert on.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

import pytest

import cli.commands as _cli
from cli.commands import _root_driver as _root_mod
from cli.commands._probe import ReadinessWait
from cli.commands._repo import ServiceSpec
from cli.commands._session_lifecycle import LaunchOutcome
from ops.service_spec import _GATEWAY
from shared.exit_codes import SERVICES_NOT_READY_EXIT_CODE


def _spec(service: str) -> ServiceSpec:
    return ServiceSpec(
        session=service,
        cmd="x",
        capabilities=_GATEWAY,
        requires_db=False,
        curl_url="http://localhost:1/",
    )


def _unit(state: str = "running", **extra: object) -> dict[str, Any]:
    return {"state": state, "desired": "running", "last_error": None, **extra}


def _status(
    units: dict[str, dict[str, Any]], *, health: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "root": {"pid": 4242, "running": True},
        "units": [{"id": unit_id, **unit} for unit_id, unit in units.items()],
        "health": health or {},
    }


class _FakeRootClient:
    """A scripted stand-in for the K1 client: status answers rotate, verbs record."""

    def __init__(self, statuses: list[dict[str, Any]]) -> None:
        self._statuses = statuses
        self.calls: list[tuple[str, str | None]] = []
        self.up_calls: list[str] = []
        self.down_calls: list[str] = []

    def status(self) -> dict[str, Any]:
        self.calls.append(("status", None))
        result = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        return {"ok": True, "result": result}

    def up(self, name: str) -> dict[str, Any]:
        self.calls.append(("up", name))
        self.up_calls.append(name)
        return {"ok": True, "result": {"verb": "up"}}

    def down(self, name: str) -> dict[str, Any]:
        self.calls.append(("down", name))
        self.down_calls.append(name)
        return {"ok": True, "result": {"verb": "down"}}


class _NoRootClient:
    """The K1 client with nothing answering (the root is down)."""

    def status(self) -> dict[str, Any]:
        from services.ava_root.client import RootClientError

        raise RootClientError("unreachable in test")


@pytest.fixture
def roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """Point the module at a tmp run dir and no helper, with a client seam."""
    monkeypatch.setattr("shared.paths.root_run_dir", lambda: tmp_path)
    monkeypatch.setattr("shared.paths.root_manifests_path", lambda: tmp_path / "manifests.json")
    monkeypatch.setattr(
        _root_mod,
        "_write_tree_manifests",
        lambda *_a, **_k: tmp_path / "manifests.json",  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_root_mod, "_helper_spawn_committed", lambda: False)
    monkeypatch.setattr(_root_mod, "_poll_sleep", lambda _s: None)  # pyright: ignore[reportUnknownArgumentType]
    box: dict[str, Any] = {}
    monkeypatch.setattr(_root_mod, "_root_client", lambda **_k: box["client"])  # pyright: ignore[reportUnknownArgumentType]
    return box


# ─── the switch + roster ────────────────────────────────────────────────────


def test_switch_defaults_off_and_env_flips(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    assert _root_mod._root_driven_enabled() is False
    monkeypatch.setattr(settings.services, "root_driver_enabled", True)
    assert _root_mod._root_driven_enabled() is True


def test_tree_roster_drops_absorbed_watchdogs_and_respects_the_skip() -> None:
    from services.ava_root_glue.manifests import ABSORBED_WATCHDOGS

    roles = frozenset({"gateway", "agent-runner"})
    roster = _root_mod._root_tree_roster(roles, {"labeler"})
    names = {spec.session for spec in roster}
    assert not (names & set(ABSORBED_WATCHDOGS))
    assert "labeler" not in names
    assert "gateway" in names


# ─── ensure_root: adopt / reconcile / replace ───────────────────────────────


def test_adopts_a_running_root_without_respawning(
    roots: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    roster = (_spec("gateway"), _spec("frontend"))
    roots["client"] = _FakeRootClient([_status({"gateway": _unit(), "frontend": _unit()})])
    spawns: list[object] = []
    monkeypatch.setattr(_root_mod, "_spawn_direct", lambda *a, **_k: spawns.append(a))  # pyright: ignore[reportUnknownArgumentType]

    outcome = _root_mod._ensure_root_service_tree(
        roster, Path("/repo"), roles=frozenset({"gateway"}), reconcile=True
    )

    assert outcome.started == roster
    assert outcome.failed == ()
    assert spawns == []
    assert roots["client"].up_calls == []
    assert roots["client"].down_calls == []


def test_operator_start_brings_down_stale_units(roots: dict[str, Any]) -> None:
    roster = (_spec("gateway"),)
    roots["client"] = _FakeRootClient([_status({"gateway": _unit(), "labeler": _unit()})])

    _root_mod._ensure_root_service_tree(
        roster, Path("/repo"), roles=frozenset({"gateway"}), reconcile=True
    )

    assert roots["client"].down_calls == ["labeler"]


def test_internal_restart_leaves_stale_units_alone(roots: dict[str, Any]) -> None:
    roster = (_spec("gateway"),)
    roots["client"] = _FakeRootClient([_status({"gateway": _unit(), "labeler": _unit()})])

    _root_mod._ensure_root_service_tree(
        roster, Path("/repo"), roles=frozenset({"gateway"}), reconcile=False
    )

    assert roots["client"].down_calls == []


def test_stopped_unit_is_brought_up(roots: dict[str, Any]) -> None:
    roster = (_spec("gateway"), _spec("heartbeat"))
    roots["client"] = _FakeRootClient(
        [_status({"gateway": _unit(), "heartbeat": {"state": "stopped", "desired": "stopped"}})]
    )

    _root_mod._ensure_root_service_tree(
        roster, Path("/repo"), roles=frozenset({"gateway"}), reconcile=True
    )

    assert roots["client"].up_calls == ["heartbeat"]


def test_missing_desired_unit_replaces_the_generation(
    roots: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    roster = (_spec("gateway"), _spec("labeler"))
    roots["client"] = _FakeRootClient(
        [
            _status({"gateway": _unit()}),
            _status({"gateway": _unit(), "labeler": _unit()}),
        ]
    )
    events: list[str] = []
    monkeypatch.setattr(
        _root_mod,
        "_stop_root_process",
        lambda *_a, **_k: events.append("stop"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _root_mod,
        "_bring_up_root",
        lambda *_a, **_k: events.append("up") or _status({"gateway": _unit(), "labeler": _unit()}),  # pyright: ignore[reportUnknownArgumentType]
    )

    outcome = _root_mod._ensure_root_service_tree(
        roster, Path("/repo"), roles=frozenset({"gateway"}), reconcile=True
    )

    assert events == ["stop", "up"]
    assert outcome.failed == ()


def test_bring_up_failure_reports_every_unit_failed(
    roots: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    roster = (_spec("gateway"), _spec("frontend"))
    roots["client"] = _NoRootClient()

    def _boom(*_a: object, **_kw: object) -> object:
        raise _root_mod._RootDriverError("no root for you")

    monkeypatch.setattr(_root_mod, "_bring_up_root", _boom)

    outcome = _root_mod._ensure_root_service_tree(
        roster, Path("/repo"), roles=frozenset({"gateway"}), reconcile=True
    )

    assert outcome.started == roster
    assert outcome.failed == ("ava-gateway", "ava-frontend")
    assert "no root for you" in capsys.readouterr().err


def test_spawn_failed_units_are_named(roots: dict[str, Any]) -> None:
    roster = (_spec("gateway"), _spec("labeler"))
    roots["client"] = _FakeRootClient(
        [
            _status(
                {
                    "gateway": _unit(),
                    "labeler": {
                        "state": "backoff",
                        "desired": "running",
                        "last_error": "spawn failed: nope",
                    },
                }
            )
        ]
    )

    outcome = _root_mod._ensure_root_service_tree(
        roster, Path("/repo"), roles=frozenset({"gateway"}), reconcile=True
    )

    assert outcome.failed == ("ava-labeler",)


def test_direct_spawn_argv_is_the_k3_face(
    roots: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: list[dict[str, Any]] = []

    class _FakePopen:
        pid = 777
        returncode = None

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            created.append({"argv": argv, **kwargs})

        def poll(self) -> None:
            return None

    class _SubprocessShim:
        PIPE = -1
        STDOUT = -2
        DEVNULL = -3
        Popen = _FakePopen

    monkeypatch.setattr(_root_mod, "subprocess", _SubprocessShim())
    monkeypatch.setattr(_root_mod, "_root_child_env", lambda: {"AVA_HOME": "/tmp/home"})  # noqa: S108 — a fake path, never created
    roots["client"] = _FakeRootClient([_status({})])

    _root_mod._bring_up_root(tmp_path, Path("/repo"), tmp_path / "manifests.json", roots["client"])

    argv = created[0]["argv"]
    assert argv[1:3] == ["-m", "services.ava_root"]
    assert "--run-dir" in argv and "--manifests" in argv and "--wiring" in argv
    assert created[0]["start_new_session"] is True


def test_helper_refuses_a_loud_fall_back(
    roots: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_root_mod, "_helper_spawn_committed", lambda: True)
    monkeypatch.setattr(_root_mod, "_helper_wire_ok", lambda: False)
    roots["client"] = _NoRootClient()

    with pytest.raises(_root_mod._RootDriverError, match="refusing to fall back"):
        _root_mod._bring_up_root(
            Path("/run"), Path("/repo"), Path("/run/manifests.json"), roots["client"]
        )


def test_helper_branch_seeds_with_the_daemon_config(
    roots: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seeds: list[dict[str, Any]] = []

    class _HelperClientShim:
        @staticmethod
        def seed_root(config: dict[str, Any], **_k: Any) -> dict[str, Any]:
            seeds.append(config)
            return {"state": "running", "seeded": True, "restarts": 0, "stop_requested": False}

    monkeypatch.setattr(_root_mod, "_helper_spawn_committed", lambda: True)
    monkeypatch.setattr(_root_mod, "_helper_wire_ok", lambda: True)
    monkeypatch.setattr(_root_mod, "_root_child_env", lambda: {"AVA_HOME": "/tmp/home"})  # noqa: S108 — a fake path, never created

    import services.permissions_helper.client as _helper_client

    monkeypatch.setattr(_helper_client, "seed_root", _HelperClientShim.seed_root)
    roots["client"] = _FakeRootClient([_status({})])

    _root_mod._bring_up_root(
        Path("/run"), Path("/repo"), Path("/run/manifests.json"), roots["client"]
    )

    assert seeds and seeds[0]["run_dir"] == "/run"
    assert seeds[0]["argv"][1:3] == ["-m", "services.ava_root"]
    assert seeds[0]["stdout"] == "/run/root.stdout.log"


# ─── readiness on the root status surface ───────────────────────────────────


def _wait(
    roots: dict[str, Any], specs: tuple[ServiceSpec, ...], timeout_s: float = 0.0
) -> ReadinessWait:
    return _root_mod._wait_for_root_services_ready(specs, timeout_s)


def test_ready_roster_passes_immediately(roots: dict[str, Any]) -> None:
    roots["client"] = _FakeRootClient(
        [
            _status(
                {"gateway": _unit(), "labeler": _unit()},
                health={"gateway": {"last_verdict": "alive"}},
            )
        ]
    )
    wait = _wait(roots, (_spec("gateway"), _spec("labeler")))
    assert wait.unready == () and wait.non_critical_unready == ()


def test_running_but_verdict_down_is_unready(roots: dict[str, Any]) -> None:
    roots["client"] = _FakeRootClient(
        [
            _status(
                {"gateway": _unit()},
                health={"gateway": {"last_verdict": "down", "last_detail": "not serving"}},
            )
        ]
    )
    wait = _wait(roots, (_spec("gateway"),), timeout_s=0.0)
    assert [s.session for s in wait.unready] == ["gateway"]
    assert wait.sessions_gone is False


def test_running_but_verdict_port_taken_is_unready(roots: dict[str, Any]) -> None:
    roots["client"] = _FakeRootClient(
        [
            _status(
                {"gateway": _unit()},
                health={
                    "gateway": {"last_verdict": "port-taken", "last_detail": "another listener"}
                },
            )
        ]
    )
    wait = _wait(roots, (_spec("gateway"),), timeout_s=0.0)
    assert [s.session for s in wait.unready] == ["gateway"]
    assert wait.sessions_gone is False


def test_a_spawn_failing_critical_is_a_gone_verdict(roots: dict[str, Any]) -> None:
    roots["client"] = _FakeRootClient(
        [
            _status(
                {
                    "gateway": {
                        "state": "backoff",
                        "desired": "running",
                        "last_error": "spawn failed: nope",
                    }
                }
            )
        ]
    )
    wait = _wait(roots, (_spec("gateway"),), timeout_s=30.0)
    assert [s.session for s in wait.unready] == ["gateway"]
    assert wait.sessions_gone is True
    assert wait.elapsed_s < 5.0  # the early exit, not the bound


def test_non_critical_failure_never_gates(
    roots: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shared.deploy_timing.NON_CRITICAL_SERVICE_READY_TIMEOUT_S", 0.0)
    roots["client"] = _FakeRootClient(
        [_status({"gateway": _unit(), "labeler": {"state": "stopped", "desired": "running"}})]
    )
    wait = _wait(roots, (_spec("gateway"), _spec("labeler")), timeout_s=0.0)
    assert wait.unready == ()
    assert [s.session for s in wait.non_critical_unready] == ["labeler"]


# ─── the stop leg ───────────────────────────────────────────────────────────


def test_stop_without_a_root_is_a_noop(
    roots: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    roots["client"] = _NoRootClient()
    _root_mod._stop_root_service_tree(preserve=frozenset(), timeout_s=1.0)
    assert "not running" in capsys.readouterr().out


def test_stop_preserved_units_stay_up(
    roots: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    roots["client"] = _FakeRootClient(
        [_status({"gateway": _unit(), "browser": _unit(), "frontend": _unit()})]
    )
    torn_down: list[object] = []
    monkeypatch.setattr(_root_mod, "_stop_root_process", lambda *a, **_k: torn_down.append(a))  # pyright: ignore[reportUnknownArgumentType]

    _root_mod._stop_root_service_tree(preserve=frozenset({"browser"}), timeout_s=1.0)

    assert roots["client"].down_calls == ["frontend", "gateway"]
    assert torn_down == []


def test_stop_without_preserve_tears_the_root_down(
    roots: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    roots["client"] = _FakeRootClient([_status({"gateway": _unit()})])
    torn_down: list[object] = []
    monkeypatch.setattr(_root_mod, "_stop_root_process", lambda *a, **_k: torn_down.append(a))  # pyright: ignore[reportUnknownArgumentType]

    _root_mod._stop_root_service_tree(preserve=frozenset(), timeout_s=1.0)

    assert torn_down, "a full stop must stop the root process itself"
    assert roots["client"].down_calls == []


def test_stop_root_process_sigterms_a_direct_root(
    roots: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    state = {"alive": True}
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(_root_mod, "_root_status", lambda _client: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("shared.proc.process_alive", lambda _pid: state["alive"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        os,
        "kill",
        lambda pid, sig: (killed.append((pid, sig)), state.update(alive=False)),  # pyright: ignore[reportUnknownArgumentType]
    )

    _root_mod._stop_root_process(
        Path("/run"),
        roots.get("client") or _NoRootClient(),
        _status({"gateway": _unit()}),
        timeout_s=1.0,
    )

    assert killed == [(4242, signal.SIGTERM)]


# ─── the config field ───────────────────────────────────────────────────────


def test_root_driver_field_shape_and_default_off() -> None:
    from shared.config import FIELD_INFOS, field_alias
    from shared.config.services import ServiceSettings

    assert ServiceSettings.model_fields["root_driver_enabled"].default is False
    assert field_alias("root_driver_enabled") == "AVA_ROOT_DRIVER_ENABLED"
    assert FIELD_INFOS["root_driver_enabled"].json_schema_extra == {
        "capability": "common",
        "restart_required": "",
        "writable": False,
        "sensitive": False,
        "scope": "host",
        "remote_writable": True,
    }


# ─── the fork inside cmd_start ──────────────────────────────────────────────


class _FakeResult:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


class _FakeSessionBackend:
    def __init__(self) -> None:
        self.alive: set[str] = set()
        self.created: list[str] = []

    def has_session(self, name: str) -> bool:
        return name in self.alive

    def new_session(self, name: str, _cmd: str, _cwd: object, *, env: object, **_: object) -> bool:
        self.created.append(name)
        return True

    def kill_session(
        self, _name: str, *, graceful: bool = False, expected: bool = False, **_: object
    ) -> tuple[bool, str]:
        return True, "forced"

    def list_sessions(self, prefix: str = "") -> list[str]:
        return sorted(n for n in self.alive if n.startswith(prefix))


def _stub_start_preconditions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything `_cmd_start_body` needs before the launch fork is decided."""
    import subprocess

    import shared.session_backend as _sb
    from cli.commands import _session_lifecycle as _session_mod
    from cli.commands import start as _start_mod

    monkeypatch.setattr(
        _cli,
        "_collect_setup_values",
        lambda _a: (  # pyright: ignore[reportUnknownArgumentType]
            {
                "machine_name": "test-machine",
                "machine_role": "gateway",
                "memory_remote": "git@github.com:test/AvaMemory.git",
                "gateway_url": "http://test-gateway:8000",
            },
            [],
        ),
    )
    monkeypatch.setattr(_cli, "converge_host", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_register_machine_or_die", lambda _r, _role: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_probe_gateway_or_die", lambda _url: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_cli, "_assert_schema_current_or_die", lambda: 0)
    monkeypatch.setattr(_cli, "_roles_or_none", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr(_start_mod, "_ensure_gateway_data_plane", lambda: 0)
    monkeypatch.setattr(_start_mod, "cmd_migrations_apply", lambda: None)
    monkeypatch.setattr(_start_mod, "cmd_status", lambda: None)
    monkeypatch.setattr(_start_mod, "SERVICE_READY_TIMEOUT_S", 0.0)
    monkeypatch.setattr(_start_mod, "_update_in_flight", lambda: False)
    monkeypatch.setattr(_session_mod, "_ensure_frontend_deps", lambda _repo: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_kw: _FakeResult())  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_sb, "get_backend", _FakeSessionBackend)
    monkeypatch.setattr(_sb, "get_shell_backend", _FakeSessionBackend)


def test_start_switch_off_uses_the_session_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_start_preconditions(monkeypatch)

    session_calls: list[object] = []

    def _fake_launch(roles: object, skip: object, repo: object) -> LaunchOutcome:
        session_calls.append((roles, skip, repo))
        return LaunchOutcome((), ())

    monkeypatch.setattr(_cli, "_launch_sessions", _fake_launch)
    monkeypatch.setattr(
        _cli,
        "_ensure_root_service_tree",
        lambda *_a, **_k: pytest.fail("the root path must not run with the switch off"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli,
        "_wait_for_services_ready",
        lambda *_a, **_k: ReadinessWait((), 0.0, sessions_gone=False),  # pyright: ignore[reportUnknownArgumentType]
    )

    rc = _cli.cmd_start()

    assert rc == 0
    assert len(session_calls) == 1


def test_start_switch_on_uses_the_root_legs(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_start_preconditions(monkeypatch)
    from shared import launch_failures

    monkeypatch.setattr(_cli, "_root_driven_enabled", lambda: True)
    root_calls: list[dict[str, object]] = []

    def _fake_root(
        roster_arg: tuple[ServiceSpec, ...], repo_arg: Path, *, roles: object, reconcile: bool
    ) -> LaunchOutcome:
        root_calls.append({"roster": roster_arg, "reconcile": reconcile})
        return LaunchOutcome(roster_arg, ("ava-gateway",))

    monkeypatch.setattr(_cli, "_ensure_root_service_tree", _fake_root)
    monkeypatch.setattr(
        _cli,
        "_launch_sessions",
        lambda *_a, **_k: pytest.fail("the session path must not run with the switch on"),  # pyright: ignore[reportUnknownArgumentType]
    )
    wait_calls: list[tuple[object, float]] = []
    monkeypatch.setattr(
        _cli,
        "_wait_for_root_services_ready",
        lambda specs, timeout_s: (  # pyright: ignore[reportUnknownArgumentType]
            wait_calls.append((specs, timeout_s)),  # pyright: ignore[reportUnknownArgumentType]
            ReadinessWait((), 0.0, sessions_gone=False),
        )[1],
    )
    recorded: list[list[str]] = []
    monkeypatch.setattr(launch_failures, "record", lambda names: recorded.append(list(names)))  # pyright: ignore[reportUnknownArgumentType]

    rc = _cli.cmd_start()

    assert root_calls and root_calls[0]["reconcile"] is True
    assert wait_calls, "the root wait must be the readiness leg"
    assert recorded == [["ava-gateway"]]
    assert rc == SERVICES_NOT_READY_EXIT_CODE
