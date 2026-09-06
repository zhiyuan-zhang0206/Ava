"""In-process update/restart legs refuse to run from inside a supervised session.

2026-08-12 incident: an agent ran `ava cluster update --local` in its pty-hosted
background shell; the orchestration's own stop leg force-killed
ava-pty-supervisor — whole tree, rollout included — and stranded the cluster
paused with every service down. The refusal makes that structurally impossible:
every in-process leg checks its own lineage against `$AVA_HOME/run/sessions/`
records (`shared.proc.hosting_supervised_session`) and bounces to the detached
form. The detached orchestration sessions themselves are exempt — they are the
sanctioned auto-updater shape: the trigger returns immediately, and a process
outside every stopped tree does the stop/update/start.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import psutil
import pytest

from cli import commands as _cli
from shared.paths import run_dir
from shared.proc import _ORCHESTRATION_SESSIONS
from shared.session_record import SessionRecord

_HOSTING_SESSION = "ava-agent-9-shell-1-t"


def test_exempt_set_matches_the_spawn_sides_session_names() -> None:
    """`shared.proc._ORCHESTRATION_SESSIONS` spells the detached session names as
    literals (the module sits below `shared.cluster` in the import graph and
    cannot compose them); the spawn side owns the service names in
    `ops/cluster_session.py`. A rename that moves one without the other would
    make the renamed detached session refuse ITSELF — its record pid is always
    in its own lineage — failing every subsequent rollout with CI green. Tests
    have no layering constraint, so this is the one place both sides meet."""
    from ops.cluster_session import (
        _CLUSTER_RESTART_SERVICE,
        _ROLLOUT_DRYRUN_SERVICE,
        _ROLLOUT_SERVICE,
        _UPDATER_SERVICE,
    )
    from shared.cluster import session_name

    spawn_side = {
        session_name(service)
        for service in (
            _ROLLOUT_SERVICE,
            _ROLLOUT_DRYRUN_SERVICE,
            _UPDATER_SERVICE,
            _CLUSTER_RESTART_SERVICE,
        )
    }
    assert spawn_side == _ORCHESTRATION_SESSIONS


def _fail_if_called(what: str) -> Callable[..., int]:
    def _boom(*_a: object, **_kw: object) -> int:
        raise AssertionError(f"{what} must not run under the supervised-session refusal")

    return _boom


@pytest.fixture
def write_session_record() -> Iterator[Callable[..., Path]]:
    """Write a live-looking session record for THIS process (or a given pid) into
    the test home's `run/sessions/`, removed again at teardown so the shared test
    home never leaks a record into other tests."""
    written: list[Path] = []
    sessions = run_dir() / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)

    def _write(name: str, *, pid: int | None = None, create_time: float | None = None) -> Path:
        pid = os.getpid() if pid is None else pid
        create_time = psutil.Process(pid).create_time() if create_time is None else create_time
        path = sessions / f"{name}.json"
        SessionRecord(
            pid=pid, create_time=create_time, cmd="test", cwd=".", started_at=time.time()
        ).write(path)
        written.append(path)
        return path

    yield _write
    for path in written:
        path.unlink(missing_ok=True)


def _stub_all_legs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.machine.machine_name", lambda: "m1")
    monkeypatch.setattr(
        _cli, "_run_gateway_orchestration", _fail_if_called("the gateway orchestration")
    )
    monkeypatch.setattr(
        _cli, "_run_agent_runner_self_update", _fail_if_called("the agent-runner self-update")
    )


@pytest.mark.parametrize(
    ("local", "restart_only"),
    [
        (True, False),  # the incident's shape: `ava cluster update --local`
        (True, True),  # --local --restart-only keeps the in-process restart-only leg
    ],
    ids=["gateway-local", "gateway-local-restart-only"],
)
def test_in_process_legs_refused_inside_supervised_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    write_session_record: Callable[..., Path],
    local: bool,
    restart_only: bool,
) -> None:
    """The in-process --local route refuses with exit 2, runs nothing, names
    the hosting session, and points at the detached form. (The bare and
    --restart-only legs are POSTs to the gateway now — issue #216 — so they
    never run a stop leg in this process and have nothing to refuse.)"""
    write_session_record(_HOSTING_SESSION)
    _stub_all_legs(monkeypatch)

    rc = _cli.cmd_update(local=local, restart_only=restart_only)

    assert rc == 2
    err = capsys.readouterr().err
    assert _HOSTING_SESSION in err
    assert "ava cluster update" in err  # the way out is named, not implied


def test_restart_only_posts_even_inside_supervised_session(
    monkeypatch: pytest.MonkeyPatch,
    write_session_record: Callable[..., Path],
) -> None:
    """--restart-only POSTs the gateway (issue #216) — the gateway runs the
    bounce detached, so a supervised caller is NOT refused; it would have been
    in-process before."""

    class _Resp:
        status_code = 202

        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"session": "ava-cluster-restart", "log": "/l"}

    write_session_record(_HOSTING_SESSION)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.post", lambda *_a, **_k: _Resp())  # pyright: ignore[reportUnknownArgumentType]

    assert _cli.cmd_update(restart_only=True) == 0


def test_local_restart_only_runs_gateway_orchestration_without_posting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The detached restart session's `--local --restart-only` route enters
    the gateway orchestration directly; the plain form above remains a POST."""
    _stub_in_process_gateway_leg(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def _capture(
        repo: Path,
        *,
        restart_only: bool,
        origin: str,
        rollout_log: str | None,
        mode: str,
        dry_run: bool,
    ) -> int:
        captured.update(
            repo=repo,
            restart_only=restart_only,
            origin=origin,
            rollout_log=rollout_log,
            mode=mode,
            dry_run=dry_run,
        )
        return 0

    monkeypatch.setattr(_cli, "_run_gateway_orchestration", _capture)
    monkeypatch.setattr(
        "cli.commands._update_dispatch._post_cluster_restart",
        _fail_if_called("the gateway restart POST"),
    )

    assert (
        _cli.cmd_update(local=True, restart_only=True, origin="detached:restart", mode="force") == 0
    )
    assert captured == {
        "repo": tmp_path,
        "restart_only": True,
        "origin": "detached:restart",
        "rollout_log": None,
        "mode": "force",
        "dry_run": False,
    }


def test_ancestor_lineage_is_walked_not_just_self(
    monkeypatch: pytest.MonkeyPatch,
    write_session_record: Callable[..., Path],
) -> None:
    """The incident's real shape: the recorded session pid is an ANCESTOR (the
    pty shell), not the update process itself."""
    write_session_record(_HOSTING_SESSION, pid=os.getppid())
    _stub_all_legs(monkeypatch)

    assert _cli.cmd_update(local=True) == 2


def _stub_in_process_gateway_leg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Let the in-process gateway dispatch reach a stubbed orchestration."""

    def _no_record(_home: Path) -> None:
        return None

    def _ok(*_a: object, **_kw: object) -> int:
        return 0

    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr("shared.machine.machine_name", lambda: "m1")
    monkeypatch.setattr("cli.commands.update._repo_root", lambda: tmp_path)
    monkeypatch.setattr("cli.commands.update.ava_home", lambda: tmp_path)
    monkeypatch.setattr("cli.commands.update.get_record", _no_record)
    monkeypatch.setattr(_cli, "_run_gateway_orchestration", _ok)


@pytest.mark.parametrize("session", sorted(_ORCHESTRATION_SESSIONS - {"ava-rollout-dryrun"}))
def test_detached_orchestration_sessions_are_exempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_session_record: Callable[..., Path],
    session: str,
) -> None:
    """The detached sessions run `ava cluster update --local` themselves — the guard
    must wave exactly them through or no rollout could ever run."""
    write_session_record(session)
    _stub_in_process_gateway_leg(monkeypatch, tmp_path)

    assert _cli.cmd_update(local=True) == 0


def test_detached_rollout_dry_run_session_is_exempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_session_record: Callable[..., Path],
) -> None:
    """The renamed detached dry-run must reach its non-mutating local leg."""
    write_session_record("ava-rollout-dryrun")
    _stub_in_process_gateway_leg(monkeypatch, tmp_path)

    assert _cli.cmd_update(local=True, dry_run=True) == 0


def test_stale_record_of_a_recycled_pid_does_not_refuse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_session_record: Callable[..., Path],
) -> None:
    """A record whose pid now belongs to a different process (start-time
    mismatch) is a dead session, not a host — same rule as the supervisors'."""
    write_session_record(_HOSTING_SESSION, create_time=1.0)
    _stub_in_process_gateway_leg(monkeypatch, tmp_path)

    assert _cli.cmd_update(local=True) == 0


def test_record_missing_create_time_fails_open_by_design(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A record without `create_time` parses as 0.0 (`SessionRecord.read`'s
    default) and can never match a live process, so the guard reads it as dead
    and lets the update through. No such record shape exists in the wild — both
    supervisors always write the field — so this pins the current fail-open as a
    deliberate choice: if the read default or the record shape ever changes,
    this test forces the change to re-decide the guard's verdict rather than
    drift it."""
    import json

    path = run_dir() / "sessions" / f"{_HOSTING_SESSION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": os.getpid(), "cmd": "t", "cwd": ".", "started_at": 0.0}))
    try:
        _stub_in_process_gateway_leg(monkeypatch, tmp_path)
        assert _cli.cmd_update(local=True) == 0
    finally:
        path.unlink(missing_ok=True)


def test_restart_refused_inside_supervised_session(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    write_session_record: Callable[..., Path],
) -> None:
    """`ava restart` severs itself the same way mid stop→start; it declines with
    the host untouched (RESTART_DECLINED_EXIT_CODE — the no-recovery verdict)."""
    from shared.exit_codes import RESTART_DECLINED_EXIT_CODE

    write_session_record(_HOSTING_SESSION)
    monkeypatch.setattr("cli.commands.stop._release_self_heal_pause", lambda: None)
    monkeypatch.setattr(_cli, "_preflight_probes", _fail_if_called("the preflight"))
    monkeypatch.setattr(_cli, "_do_stop", _fail_if_called("_do_stop"))

    rc = _cli.cmd_restart()

    assert rc == RESTART_DECLINED_EXIT_CODE
    assert _HOSTING_SESSION in capsys.readouterr().err


def test_restart_proceeds_when_windows_stop_would_spare_its_lineage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    write_session_record: Callable[..., Path],
) -> None:
    """The updater remains alive when stopping its Windows ops-daemon parent.

    Windows keeps the updater beneath ``ava-ops`` rather than reparenting it, but
    the kill path has spared that updater subtree since the 2026-07-29 incident.
    The 2026-08-24 guard must consult that fact instead of declining a restart
    whose stop leg cannot kill it.
    """
    from shared.proc import hosting_supervised_session

    def _spares(_name: str, _proc: psutil.Process, _ancestor_pids: set[int]) -> bool:
        return True

    def _success(*_args: object, **_kwargs: object) -> int:
        return 0

    write_session_record("ava-updater", pid=os.getppid())
    write_session_record("ava-ops", pid=os.getppid())
    monkeypatch.setattr("shared.winproc.tree_kill_would_spare", _spares)
    monkeypatch.setattr(_cli, "_preflight_probes", _success)
    monkeypatch.setattr(_cli, "_do_stop", _success)
    monkeypatch.setattr(_cli, "_cmd_start_body", _success)

    assert hosting_supervised_session() is None
    assert _cli.cmd_restart() == 0
    assert "refusing restart" not in capsys.readouterr().err


def test_restart_refuses_when_the_service_tree_would_not_spare_its_lineage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    write_session_record: Callable[..., Path],
) -> None:
    """The 2026-08-12 whole-tree shape remains unsafe and must still refuse."""
    from shared.exit_codes import RESTART_DECLINED_EXIT_CODE

    def _does_not_spare(_name: str, _proc: psutil.Process, _ancestor_pids: set[int]) -> bool:
        return False

    write_session_record(_HOSTING_SESSION, pid=os.getppid())
    monkeypatch.setattr("shared.winproc.tree_kill_would_spare", _does_not_spare)
    monkeypatch.setattr("cli.commands.stop._release_self_heal_pause", lambda: None)
    monkeypatch.setattr(_cli, "_preflight_probes", _fail_if_called("the preflight"))
    monkeypatch.setattr(_cli, "_do_stop", _fail_if_called("_do_stop"))

    assert _cli.cmd_restart() == RESTART_DECLINED_EXIT_CODE
    assert _HOSTING_SESSION in capsys.readouterr().err


def test_pty_session_records_do_not_refuse() -> None:
    """A pty session record for this very process (run/pty, the per-session
    host namespace) must NOT read as a supervised session: the guard's scan is
    `run/sessions/` only. Under per-session hosts an update running inside an
    agent shell is structurally safe — the update's stop scope contains no pty
    target, so nothing it stops can kill the shell hosting it — and silently
    widening the scan to run/pty would re-refuse exactly the case the
    architecture made legal (pinned here so a future sweep cannot regress it).
    """
    from shared.proc import hosting_supervised_session
    from shared.pty_sessions._paths import write_record

    pty_ns = run_dir() / "pty"
    pty_ns.mkdir(parents=True, exist_ok=True)
    path = pty_ns / "ava-agent-1-shell-1-guard-test.json"
    me = os.getpid()
    write_record(
        path,
        SessionRecord(
            pid=me,
            create_time=psutil.Process(me).create_time(),
            cmd="/bin/bash -l -i",
            cwd=".",
            started_at=time.time(),
        ),
        host_pid=me,
        host_create_time=psutil.Process(me).create_time(),
    )
    try:
        assert hosting_supervised_session() is None
    finally:
        path.unlink(missing_ok=True)
