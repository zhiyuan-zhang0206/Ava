"""Tests for shared.posixproc — the native POSIX process supervisor that hosts
detached agent processes. These spawn REAL child processes (the double-fork
reparent path is the whole point), so they are POSIX-only and use the unit_home
fixture to keep records/logs under a tmp $AVA_HOME.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from shared import posixproc
from shared.platform import IS_LINUX, IS_WINDOWS
from shared.session_record import SessionRecord, pid_starttime_ticks

pytestmark = pytest.mark.skipif(IS_WINDOWS, reason="posixproc is the POSIX supervisor")

# A long-lived child that outlives the test body; each test kills it explicitly.
_SLEEP = ["/bin/sleep", "300"]


def _new(name: str, argv: list[str], cwd: Path) -> bool:
    """Launch via posixproc with the inherited env (children need PATH etc.)."""
    return posixproc.new_session(name, argv, cwd, env=dict(os.environ))


def _pid(name: str) -> int:
    """The recorded pid for a live session (asserts the record exists)."""
    rec = posixproc._read_record(name)
    assert rec is not None
    return rec.pid


def _wait(predicate, timeout: float = 5.0, interval: float = 0.05) -> bool:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()  # pyright: ignore[reportUnknownVariableType]


def test_new_session_reparents_to_init_no_zombie(unit_home) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """The launched child double-forks: it reparents to init (PPID==1) immediately,
    so the spawner (this test process) accretes no zombie, and the record tracks
    the real child pid."""
    name = "ava-test-agent-1"
    spawner = psutil.Process()
    before_children = set(spawner.children())

    assert _new(name, list(_SLEEP), unit_home) is True  # pyright: ignore[reportUnknownArgumentType]
    try:
        rec = posixproc._read_record(name)
        assert rec is not None
        assert rec.starttime == pid_starttime_ticks(rec.pid)
        child = psutil.Process(rec.pid)
        # The record is written the instant the reparent helper reports the
        # grandchild pid, which can be microseconds BEFORE that grandchild
        # execvp's into /bin/sleep — until then psutil reads the pre-exec
        # `python -m shared._reparent` image. Wait for the exec to land instead
        # of sampling the name once (that one-shot read flakes on a loaded runner).
        assert _wait(lambda: child.name() == "sleep"), (
            f"child never became sleep (name={child.name()})"
        )
        # Reparented to init — the double-fork's payoff (init reaps it on death).
        # The reparent lands when the helper (the child's parent) exits, which
        # new_session's subprocess.run has already awaited; poll rather than
        # one-shot so a beat of ppid-update lag on a loaded runner can't flake it
        # (same treatment as the exec-name wait above).
        assert _wait(lambda: child.ppid() == 1), (
            f"child never reparented to init (ppid={child.ppid()})"
        )
        assert posixproc.has_session(name) is True
        assert name in posixproc.list_sessions(prefix="ava-test-agent-")

        # The spawner gained no lingering direct child (the helper was reaped by
        # the internal subprocess wait), and no zombie.
        new_children = set(spawner.children()) - before_children
        assert new_children == set()
        zombies = [
            c for c in spawner.children(recursive=True) if c.status() == psutil.STATUS_ZOMBIE
        ]
        assert zombies == []
    finally:
        posixproc.kill_session(name, graceful=False)


def test_graceful_signal_terminates(unit_home) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """graceful_signal sends SIGTERM; a default-disposition child dies, so the
    session goes away (the reap's O(slowest-agent) shared-deadline path)."""
    name = "ava-test-agent-2"
    _new(name, list(_SLEEP), unit_home)  # pyright: ignore[reportUnknownArgumentType]
    try:
        assert posixproc.graceful_signal(name) is True
        assert _wait(lambda: not posixproc.has_session(name))
    finally:
        posixproc.kill_session(name, graceful=False)


def test_kill_session_force_kills_sigterm_ignoring_survivor(unit_home) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """graceful kill_session SIGTERMs, waits, then SIGKILLs a straggler that
    ignored SIGTERM — the force floor under the graceful reap."""
    name = "ava-test-agent-3"
    # A process that ignores SIGTERM, so only SIGKILL takes it down.
    argv = [
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)",
    ]
    _new(name, argv, unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    assert _wait(lambda: psutil.pid_exists(pid))
    ok, mode = posixproc.kill_session(name, graceful=True, timeout=0.5)
    assert ok is True and mode == "graceful"
    assert _wait(lambda: not psutil.pid_exists(pid))
    assert not posixproc._record_path(name).exists()


def test_kill_session_reaps_process_tree(unit_home) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """A force kill takes down the child's own descendants too (Claude Code /
    subprocesses an agent spawns), not just the top process."""
    name = "ava-test-agent-4"
    # Parent python spawns a child `sleep` and prints its pid, then idles.
    argv = [
        sys.executable,
        "-c",
        "import subprocess,sys,time;"
        "c=subprocess.Popen(['sleep','300']);"
        "sys.stderr.write(str(c.pid));sys.stderr.flush();time.sleep(300)",
    ]
    _new(name, argv, unit_home)  # pyright: ignore[reportUnknownArgumentType]
    parent_pid = _pid(name)
    # Give the parent a moment to spawn its child, then find it.
    assert _wait(lambda: psutil.pid_exists(parent_pid) and psutil.Process(parent_pid).children())
    child_pid = psutil.Process(parent_pid).children()[0].pid

    posixproc.kill_session(name, graceful=False)
    assert _wait(lambda: not psutil.pid_exists(parent_pid))
    assert _wait(lambda: not psutil.pid_exists(child_pid)), "child sleep should be reaped with tree"


def test_has_session_false_when_process_gone(unit_home) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    name = "ava-test-agent-5"
    _new(name, list(_SLEEP), unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    posixproc.kill_session(name, graceful=False)
    assert not psutil.pid_exists(pid)
    # Record is gone after kill, so has_session is False.
    assert posixproc.has_session(name) is False


def test_list_sessions_reaps_dead_record(unit_home) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """A record whose process is gone is unlinked as list_sessions walks it, so the
    listing reflects reality (mirrors winproc)."""
    name = "ava-test-agent-6"
    _new(name, list(_SLEEP), unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    # Kill the process out from under the record WITHOUT going through kill_session,
    # so the record file lingers until list_sessions reaps it.
    # One Process handle for kill+wait: the child is reparented to init, which
    # reaps it the instant it dies — a second `psutil.Process(pid)` constructed
    # after kill() races that reap and can raise NoSuchProcess in __init__.
    # The handle pins identity at construction; wait() on an already-gone
    # non-child pid just returns.
    proc = psutil.Process(pid)
    proc.kill()
    proc.wait(timeout=5)
    assert posixproc._record_path(name).exists()
    assert posixproc.list_sessions(prefix="ava-test-agent-") == []
    assert not posixproc._record_path(name).exists()


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_starttime_identity_survives_wall_clock_drift(unit_home: Path) -> None:
    """A stable kernel start tick keeps a live record despite a bad epoch time."""
    name = "ava-test-agent-starttime-drift"
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        assert _wait(lambda: psutil.pid_exists(child.pid))
        starttime = pid_starttime_ticks(child.pid)
        assert starttime is not None
        rec = SessionRecord(
            pid=child.pid,
            create_time=1.0,
            cmd="test",
            cwd=str(unit_home),
            started_at=time.time(),
            starttime=starttime,
        )
        rec.write(posixproc._record_path(name))

        assert rec.identifies(child.pid) is True
        assert posixproc.has_session(name) is True
        assert posixproc.list_sessions(prefix="ava-test-agent-starttime-") == [name]
        assert posixproc._record_path(name).exists()
    finally:
        try:
            child.kill()
            child.wait(timeout=5)
        finally:
            posixproc._record_path(name).unlink(missing_ok=True)


@pytest.mark.skipif(not IS_LINUX, reason="Linux /proc start-time identity")
def test_list_sessions_reaps_starttime_pid_reuse(unit_home: Path) -> None:
    """A live pid with different start ticks is a recycled pid and is reaped."""
    name = "ava-test-agent-starttime-reuse"
    rec = SessionRecord(
        pid=os.getpid(),
        create_time=psutil.Process().create_time(),
        cmd="test",
        cwd=str(unit_home),
        started_at=time.time(),
        starttime=0,
    )
    rec.write(posixproc._record_path(name))

    assert rec.identifies(os.getpid()) is False
    assert posixproc.has_session(name) is False
    assert posixproc.list_sessions(prefix="ava-test-agent-starttime-") == []
    assert not posixproc._record_path(name).exists()


def test_list_sessions_keeps_live_legacy_record_with_clock_drift(
    unit_home: Path,
    loguru_records: list[dict[str, object]],
) -> None:
    """A legacy timestamp mismatch cannot make a live session unowned."""
    name = "ava-test-agent-legacy-drift"
    SessionRecord(
        pid=os.getpid(),
        create_time=1.0,
        cmd="test",
        cwd=str(unit_home),
        started_at=time.time(),
    ).write(posixproc._record_path(name))

    assert posixproc.has_session(name) is False
    assert posixproc.list_sessions(prefix="ava-test-agent-legacy-") == []
    assert posixproc._record_path(name).exists()
    assert any(
        "retaining live session record" in str(record["message"]) for record in loguru_records
    )


def test_new_session_idempotent_when_live(unit_home) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    """A second new_session for a still-live name is a no-op (returns True without
    relaunching) — matching winproc + the has-session guard at the call site."""
    name = "ava-test-agent-7"
    _new(name, list(_SLEEP), unit_home)  # pyright: ignore[reportUnknownArgumentType]
    try:
        first_pid = _pid(name)
        assert _new(name, list(_SLEEP), unit_home) is True  # pyright: ignore[reportUnknownArgumentType]
        assert _pid(name) == first_pid  # not relaunched
    finally:
        posixproc.kill_session(name, graceful=False)


def test_kill_session_noop_on_absent(unit_home) -> None:  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    assert posixproc.kill_session("ava-test-agent-does-not-exist") == (True, "noop")
    assert posixproc.graceful_signal("ava-test-agent-does-not-exist") is False


def test_kill_session_survivor_reports_and_keeps_record(
    unit_home,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """A kill that fails to take the process down returns (False, mode) and
    KEEPS the record — the no-DB reap's only view of the survivor (audit
    2026-08-08 P1; mirrors winproc's #1015 fix). The old code reported
    success unconditionally and unlinked the record, so a live-but-unbacked
    session became a service nothing starts."""
    name = "ava-test-agent-survivor"
    argv = [sys.executable, "-c", "import time; time.sleep(300)"]
    _new(name, argv, unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    assert _wait(lambda: psutil.pid_exists(pid))

    # Simulate a kill that achieves nothing (SIGKILL raced a D-state process,
    # or AccessDenied skipped _terminate_tree's whole body).
    def _noop_kill(*_args, **_kwargs):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        return None

    _real_terminate_tree = posixproc._terminate_tree
    monkeypatch.setattr(posixproc, "_terminate_tree", _noop_kill)  # pyright: ignore[reportUnknownArgumentType]
    ok, mode = posixproc.kill_session(name, graceful=False)
    assert ok is False and mode == "forced"
    assert psutil.pid_exists(pid), "the process must still be alive"
    assert posixproc._record_path(name).exists(), "record must survive so the reap sees it"

    # Real kill for cleanup (restore the real _terminate_tree first); the
    # record is then reaped normally. NOTE: never monkeypatch.undo() here —
    # it would also undo `unit_home`'s ava_home patch and the cleanup would
    # hit the real $AVA_HOME.
    monkeypatch.setattr(posixproc, "_terminate_tree", _real_terminate_tree)
    ok, _mode = posixproc.kill_session(name, graceful=False)
    assert ok is True
    assert _wait(lambda: not psutil.pid_exists(pid))
    assert not posixproc._record_path(name).exists()


def test_kill_session_terminate_error_keeps_record(
    unit_home,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """_terminate_tree raising (not just failing silently) must also report
    False and keep the record — the raise path is the same lie, one line up."""
    name = "ava-test-agent-err"
    argv = [sys.executable, "-c", "import time; time.sleep(300)"]
    _new(name, argv, unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    assert _wait(lambda: psutil.pid_exists(pid))

    def boom(*a, **k):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise OSError("kill failed")

    _real_terminate_tree = posixproc._terminate_tree
    monkeypatch.setattr(posixproc, "_terminate_tree", boom)  # pyright: ignore[reportUnknownArgumentType]
    ok, mode = posixproc.kill_session(name, graceful=False)
    assert ok is False and mode == "forced"
    assert psutil.pid_exists(pid)
    assert posixproc._record_path(name).exists()

    monkeypatch.setattr(posixproc, "_terminate_tree", _real_terminate_tree)
    ok, _mode = posixproc.kill_session(name, graceful=False)
    assert ok is True
    assert _wait(lambda: not psutil.pid_exists(pid))


def test_new_session_dead_child_records_sentinel(
    unit_home,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
    monkeypatch: pytest.MonkeyPatch,  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
) -> None:
    """A child whose create_time cannot be read — it died at spawn, freeing
    the pid at its most reusable moment — gets a sentinel that can never
    match a reused pid, not time.time() (audit 2026-08-08 P2: the old value
    made `_process_for_record` accept the pid's next innocent occupant, and a
    later kill_session could SIGKILL an unrelated process tree)."""
    name = "ava-test-agent-dying"
    real_process = psutil.Process

    class _NoCreateTime(real_process):
        def create_time(self):
            raise psutil.NoSuchProcess(self.pid)

    monkeypatch.setattr("shared.posixproc.psutil.Process", _NoCreateTime)
    argv = [sys.executable, "-c", "import time; time.sleep(300)"]
    _new(name, argv, unit_home)  # pyright: ignore[reportUnknownArgumentType]
    rec = posixproc._read_record(name)
    assert rec is not None and rec.create_time == posixproc._DEAD_CHILD_SENTINEL
    assert rec.starttime is None
    assert not posixproc.has_session(name), "sentinel record must read as dead from birth"

    # The real child is still alive (the patch only faked the create_time
    # read); clean it up directly. Restore the real Process class, not
    # monkeypatch.undo() — the latter would also undo `unit_home`'s ava_home
    # patch and the cleanup would touch the real $AVA_HOME.
    pid = rec.pid
    monkeypatch.setattr("shared.posixproc.psutil.Process", real_process)
    with contextlib.suppress(psutil.NoSuchProcess):
        psutil.Process(pid).kill()
    posixproc._record_path(name).unlink(missing_ok=True)
