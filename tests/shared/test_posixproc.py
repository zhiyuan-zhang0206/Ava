"""Tests for shared.posixproc — the native POSIX process supervisor that hosts
detached agent processes. These spawn REAL child processes (the double-fork
reparent path is the whole point), so they are POSIX-only and use the unit_home
fixture to keep records/logs under a tmp $AVA_HOME.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import pytest

from shared import posixproc
from shared.platform import IS_LINUX, IS_WINDOWS
from shared.session_record import SessionRecord, pid_starttime_ticks
from tests.shared.process_evidence import detach_evidence, detached_to_known_reaper

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


def _wait(predicate, timeout: float = 30.0, interval: float = 0.05) -> bool:
    # 30s default (2026-09-03): process-lifetime waits (group kill reaping a
    # late-spawned child, posixproc task #2249 regression test) tripped their
    # old 5s budget under CI runner oversubscription — same failure mode the
    # pty-sessions suite already sized for on 2026-08-09.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _no_new_direct_children(spawner: psutil.Process, before: set[psutil.Process]) -> bool:
    """True when `spawner` has no direct child beyond the pre-spawn snapshot."""
    try:
        return set(spawner.children()) - before == set()
    except psutil.NoSuchProcess:
        return True


def _no_zombie_children(spawner: psutil.Process) -> bool:
    """True when none of `spawner`'s recursive children is a zombie.

    Children that exit between the snapshot and the status read are skipped —
    a reaped child is exactly what the no-zombie assertion wants.
    """
    try:
        children = spawner.children(recursive=True)
    except psutil.NoSuchProcess:
        return True
    for child in children:
        try:
            if child.status() == psutil.STATUS_ZOMBIE:
                return False
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return True


def _pid_gone_or_zombie(pid: int) -> bool:
    """True when `pid` no longer exists, or is a zombie (dead, unreaped).

    psutil.pid_exists() is zombie-blind (it returns True for a zombie), and a
    SIGKILLed child lingers as a zombie until init asynchronously reaps it — so
    "process is gone" assertions must count a zombie as gone or they race init's
    reap schedule (same semantics as the orphan-reaper fix #1272).
    """
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded.*:DeprecationWarning")
def test_process_is_live_treats_zombie_as_dead() -> None:
    """A reaped-but-unreaped (zombie) child must not count as live — the
    is_running() blind spot that made kill-then-assert tests race init's reap.
    Deterministic fork+pipe probe (same pattern as the orphan-reaper regression
    test in #1272): the child writes a byte then exits; the parent reads it and
    holds the pid unreaped until the status read confirms ZOMBIE."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        os.write(write_fd, b"x")
        os._exit(0)
    os.close(write_fd)
    try:
        os.read(read_fd, 1)  # child has started its exit; parent has not waited
        proc = psutil.Process(pid)
        assert _wait(lambda: proc.status() == psutil.STATUS_ZOMBIE)
        assert posixproc._process_is_live(proc) is False
        assert (
            posixproc._process_for_record(
                SessionRecord(
                    pid=pid,
                    create_time=proc.create_time(),
                    cmd="zombie",
                    cwd="/",
                    started_at=time.time(),
                )
            )
            is None
        )
    finally:
        os.close(read_fd)
        os.waitpid(pid, 0)  # reap so the test never leaks a zombie


def test_new_session_reparents_to_init_no_zombie(
    unit_home,
    record_property: Callable[[str, object], None],
) -> None:
    """The launched child double-forks to init or an ancestor subreaper,
    so the spawner (this test process) accretes no zombie, and the record tracks
    the real child pid."""
    name = "ava-test-agent-1"
    spawner = psutil.Process()
    before_children = set(spawner.children())
    ancestor_births = {(parent.pid, parent.create_time()) for parent in spawner.parents()}
    caller_sid = os.getsid(0)

    assert _new(name, list(_SLEEP), unit_home) is True  # pyright: ignore[reportUnknownArgumentType]
    try:
        rec = posixproc._read_record(name)
        assert rec is not None
        assert rec.starttime == pid_starttime_ticks(rec.pid)
        child = psutil.Process(rec.pid)
        record_property("detach_process_evidence", json.dumps(detach_evidence(rec.pid)))
        # The record is written the instant the reparent helper reports the
        # grandchild pid, which can be microseconds BEFORE that grandchild
        # execvp's into /bin/sleep — until then psutil reads the pre-exec
        # `python -m shared._reparent` image. Wait for the exec to land instead
        # of sampling the name once (that one-shot read flakes on a loaded runner).
        assert _wait(lambda: child.name() == "sleep"), (
            f"child never became sleep (name={child.name()})"
        )
        # Init or a pre-existing ancestor subreaper adopts the detached child.
        # PID + birth guards reject an unknown/recycled adopter; the caller and
        # its session remain forbidden. Unreadable observations fail the test.
        # The reparent lands when the helper (the child's parent) exits, which
        # new_session's subprocess.run has already awaited; poll rather than
        # one-shot so a beat of ppid-update lag on a loaded runner can't flake it
        # (same treatment as the exec-name wait above).
        assert _wait(
            lambda: detached_to_known_reaper(child.pid, spawner.pid, caller_sid, ancestor_births)
        ), f"child never detached to a known reaper: {detach_evidence(child.pid)}"
        # Each of these is a state the OS lands asynchronously (record write vs
        # /proc visibility), so read it with a bounded poll instead of sampling
        # once — a one-shot read races the update on a loaded runner.
        assert _wait(lambda: posixproc.has_session(name) is True)
        assert _wait(lambda: name in posixproc.list_sessions(prefix="ava-test-agent-"))

        # The spawner gained no lingering direct child (the helper was reaped by
        # the internal subprocess wait), and no zombie. Same bounded-poll
        # treatment — the assertions themselves are unchanged.
        assert _wait(lambda: _no_new_direct_children(spawner, before_children)), (
            f"spawner gained a lingering direct child: {set(spawner.children()) - before_children}"
        )
        assert _wait(lambda: _no_zombie_children(spawner)), (
            "spawner accreted a zombie; children: "
            f"{[c.pid for c in spawner.children(recursive=True)]}"
        )
    finally:
        posixproc.kill_session(name, graceful=False)


def test_graceful_signal_terminates(unit_home) -> None:
    """graceful_signal sends SIGTERM; a default-disposition child dies, so the
    session goes away (the reap's O(slowest-agent) shared-deadline path)."""
    name = "ava-test-agent-2"
    _new(name, list(_SLEEP), unit_home)  # pyright: ignore[reportUnknownArgumentType]
    try:
        assert posixproc.graceful_signal(name) is True
        assert _wait(lambda: not posixproc.has_session(name))
    finally:
        posixproc.kill_session(name, graceful=False)


def test_kill_session_force_kills_sigterm_ignoring_survivor(unit_home: Path) -> None:
    """graceful kill_session SIGTERMs, waits, then SIGKILLs a straggler that
    ignored SIGTERM — the force floor under the graceful reap."""
    name = "ava-test-agent-3"
    # A process that ignores SIGTERM, so only SIGKILL takes it down. The marker
    # file proves SIG_IGN is installed BEFORE the kill: a SIGTERM that lands in
    # the pre-exec / handler-install window kills the child "gracefully", which
    # is not the force-floor scenario this test names (the old `mode ==
    # "graceful"` expectation passed for that wrong reason).
    marker = unit_home / "sigign-ready"
    argv = [
        sys.executable,
        "-c",
        "import signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f"open({str(marker)!r}, 'w').close();"
        "time.sleep(300)",
    ]
    _new(name, argv, unit_home)
    pid = _pid(name)
    assert _wait(lambda: psutil.pid_exists(pid))
    assert _wait(marker.exists), "child never installed SIG_IGN"
    ok, mode = posixproc.kill_session(name, graceful=True, timeout=0.5)
    # The child ignores SIGTERM, so the graceful signal alone cannot end it —
    # the force floor must fire, which is exactly what `forced` reports.
    assert ok is True and mode == "forced"
    assert _wait(lambda: _pid_gone_or_zombie(pid))
    assert not posixproc._record_path(name).exists()


def test_kill_session_reaps_process_tree(unit_home) -> None:
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
    assert _wait(lambda: _pid_gone_or_zombie(parent_pid))
    assert _wait(lambda: _pid_gone_or_zombie(child_pid)), "child sleep should be reaped with tree"


def test_has_session_false_when_process_gone(unit_home) -> None:
    name = "ava-test-agent-5"
    _new(name, list(_SLEEP), unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    posixproc.kill_session(name, graceful=False)
    # The SIGKILLed child lingers as a zombie until init reaps it — poll with a
    # zombie counted as gone instead of asserting on the one-shot pid_exists.
    assert _wait(lambda: _pid_gone_or_zombie(pid))
    # Record is gone after kill, so has_session is False.
    assert posixproc.has_session(name) is False


def test_list_sessions_reaps_dead_record(unit_home) -> None:
    """A record whose process is gone is unlinked as list_sessions walks it, so the
    listing reflects reality (mirrors winproc)."""
    name = "ava-test-agent-6"
    _new(name, list(_SLEEP), unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    # Kill the process out from under the record WITHOUT going through kill_session,
    # so the record file lingers until list_sessions reaps it.
    # The child is reparented to init, which reaps it asynchronously — wait for
    # the pid to disappear (bounded) rather than proc.wait(), which raises
    # ChildProcessError on a non-child zombie.
    proc = psutil.Process(pid)
    proc.kill()
    assert _wait(lambda: not psutil.pid_exists(pid))
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


def test_new_session_idempotent_when_live(unit_home) -> None:
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


def test_kill_session_noop_on_absent(unit_home) -> None:
    assert posixproc.kill_session("ava-test-agent-does-not-exist") == (True, "noop")
    assert posixproc.graceful_signal("ava-test-agent-does-not-exist") is False


def test_kill_session_survivor_reports_and_keeps_record(
    unit_home,
    monkeypatch: pytest.MonkeyPatch,
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
    def _noop_kill(*_args, **_kwargs):
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
    unit_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_terminate_tree raising (not just failing silently) must also report
    False and keep the record — the raise path is the same lie, one line up."""
    name = "ava-test-agent-err"
    argv = [sys.executable, "-c", "import time; time.sleep(300)"]
    _new(name, argv, unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    assert _wait(lambda: psutil.pid_exists(pid))

    def boom(*a, **k):
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
    unit_home,
    monkeypatch: pytest.MonkeyPatch,
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


def test_kill_session_group_kill_reaps_late_spawned_child(
    unit_home,
    tmp_path: Path,
    record_property: Callable[[str, object], None],
) -> None:
    """A child spawned DURING the graceful wait (an unwound finally's process,
    a bash wrapper's foreground command) shares the session's process group, so
    the group kill takes it down — the old pre-captured children walk left it
    orphaned (task #2249). The late child is the discriminating half: the top
    process spawns it half a second into the kill — strictly after any
    pre-captured children snapshot."""
    name = "ava-test-agent-late"
    ready_file = tmp_path / "ready"
    late_file = tmp_path / "late.pid"
    # The top process installs a no-op SIGTERM handler (it does NOT exit on the
    # graceful signal — the wrapper shape), announces readiness, then spawns an
    # ignore-SIGTERM child half a second in — strictly inside the graceful
    # window, after any children snapshot the old walk could have captured.
    # Single-threaded on purpose: the spawn is a plain Popen between two
    # sleeps, no def block (a one-line `def` cannot carry `;`-joined bodies).
    argv = [
        sys.executable,
        "-c",
        "import signal,subprocess,sys,time;"
        f"ready={str(ready_file)!r};"
        f"late={str(late_file)!r};"
        "signal.signal(signal.SIGTERM,lambda *a: None);"
        "open(ready,'w').write('ready');"
        "time.sleep(0.5);"
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "time.sleep(300)']);"
        "open(late,'w').write(str(p.pid));"
        "time.sleep(300)",
    ]
    _new(name, argv, unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    try:
        assert _wait(ready_file.exists), "top process never armed its SIGTERM handler"
        ok, mode = posixproc.kill_session(name, graceful=True, timeout=2.0)
        assert ok is True and mode == "forced"
        assert _wait(lambda: not psutil.pid_exists(pid)), "top process must be gone"
        assert _wait(late_file.exists), "late child never spawned"
        late_pid = int(late_file.read_text())
        record_property("late_child_evidence", json.dumps(detach_evidence(late_pid)))

        def child_exited() -> bool:
            try:
                return psutil.Process(late_pid).status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                return True

        assert _wait(child_exited), (
            f"late-spawned child must exit with the group: {detach_evidence(late_pid)}"
        )
    finally:
        posixproc.kill_session(name, graceful=False)


def test_kill_session_group_kill_reaps_sigterm_ignoring_wrapper(
    unit_home,
) -> None:
    """A bash wrapper that ignores SIGTERM (it waits for its foreground
    command) and its background child are both taken down by the group signals:
    SIGTERM first (both ignore it), then the SIGKILL group floor. Shape
    coverage, not a discriminator — the old children-snapshot walk also killed
    this pair (the child was in the snapshot); the discriminating tests are the
    late-spawned-child and detached-descendant ones."""
    name = "ava-test-agent-bashwrap"
    # `trap '' TERM` makes bash ignore SIGTERM (and the exec'd sleep inherits
    # SIG_IGN across fork+exec); `& wait` keeps a child alive under it, the
    # shape of a wrapper sitting on a foreground command.
    _new(name, ["/bin/bash", "-c", "trap '' TERM; sleep 300 & wait"], unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    try:
        assert _wait(lambda: psutil.pid_exists(pid) and psutil.Process(pid).children())
        child_pid = psutil.Process(pid).children()[0].pid
        ok, mode = posixproc.kill_session(name, graceful=True, timeout=0.5)
        assert ok is True and mode == "forced"
        assert _wait(lambda: not psutil.pid_exists(pid))
        assert _wait(lambda: not psutil.pid_exists(child_pid)), (
            "wrapper's child must die with the group"
        )
    finally:
        posixproc.kill_session(name, graceful=False)


def test_kill_session_signals_the_process_group(
    unit_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TTL path's final executor (posixproc.kill_session, reached from the
    page-server daemon's TTL reconcile) signals the session's process GROUP,
    not just the recorded pid — the regression guard for the group-kill fix
    (task #2249)."""
    name = "ava-test-agent-grp"
    _new(name, list(_SLEEP), unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    pgid = os.getpgid(pid)
    calls: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def spy(pg: int, sig: int) -> None:
        calls.append((pg, sig))
        return real_killpg(pg, sig)  # type: ignore[func-returns-value]

    monkeypatch.setattr(posixproc.os, "killpg", spy)
    try:
        ok, mode = posixproc.kill_session(name, graceful=True, timeout=2.0)
        assert ok is True and mode == "graceful"
        assert (pgid, signal.SIGTERM) in calls, "graceful kill must signal the group"
    finally:
        posixproc.kill_session(name, graceful=False)


def test_kill_session_group_kill_reaps_detached_descendant(
    unit_home,
    tmp_path: Path,
) -> None:
    """A descendant that deliberately left the session's process group (a
    setsid'd worker) is unreachable by group signal — the pre-captured
    children walk is the only handle on it, and the kill must reap it too
    (review nit on #1301; pty-orphan history)."""
    name = "ava-test-agent-detached"
    ready = tmp_path / "ready"
    detached_file = tmp_path / "detached.pid"
    argv = [
        sys.executable,
        "-c",
        "import subprocess,sys,time;"
        f"ready={str(ready)!r};"
        f"detached={str(detached_file)!r};"
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import time;time.sleep(300)'],start_new_session=True);"
        "open(detached,'w').write(str(p.pid));"
        "open(ready,'w').write('ready');"
        "time.sleep(300)",
    ]
    _new(name, argv, unit_home)  # pyright: ignore[reportUnknownArgumentType]
    pid = _pid(name)
    detached_pid = -1
    try:
        assert _wait(ready.exists)
        detached_pid = int(detached_file.read_text())
        assert _wait(lambda: psutil.pid_exists(detached_pid))
        # The descendant must really be outside the session's group, or the
        # test proves nothing (the group signal would cover it).
        assert os.getpgid(detached_pid) != os.getpgid(pid)
        ok, mode = posixproc.kill_session(name, graceful=True, timeout=1.0)
        assert ok is True and mode == "forced"  # the detached survivor forces the hard kill
        assert _wait(lambda: not psutil.pid_exists(pid))
        assert _wait(lambda: not psutil.pid_exists(detached_pid)), (
            "detached descendant must die with the session"
        )
    finally:
        posixproc.kill_session(name, graceful=False)
        # The detached descendant is outside the group, so a cleanup
        # kill_session cannot reach it — kill it directly if it survived.
        if detached_pid > 0:
            with contextlib.suppress(psutil.Error):
                psutil.Process(detached_pid).kill()


def test_process_is_live_false_for_zombie(monkeypatch: pytest.MonkeyPatch) -> None:
    """_process_is_live counts a zombie as dead awaiting its parent's reap. The
    graceful verdict must not wait on init/launchd reap latency (macmini
    failure on #1301; #1303 class)."""
    import types

    proc = types.SimpleNamespace()
    proc.is_running = lambda: True
    proc.status = lambda: psutil.STATUS_ZOMBIE
    assert posixproc._process_is_live(proc) is False  # type: ignore[arg-type]


def _group_exists(_pgid: int, _sig: int) -> None:
    """os.killpg stub: the probe group exists (no ProcessLookupError)."""
    return


def _same_group(_pid: int) -> int:
    """os.getpgid stub: every probed process belongs to the group under test."""
    return 999


def test_group_empty_ignores_zombie_members(monkeypatch: pytest.MonkeyPatch) -> None:
    """A group whose only occupants are zombies is empty for the graceful
    verdict — killpg(pgid, 0) would still succeed on it, so the fast probe is
    followed by a member walk that exempts zombies."""
    import types

    zombie = types.SimpleNamespace(pid=424242)
    zombie.status = lambda: psutil.STATUS_ZOMBIE  # type: ignore[attr-defined]
    monkeypatch.setattr(posixproc.os, "killpg", _group_exists)
    monkeypatch.setattr(posixproc.psutil, "process_iter", lambda: iter([zombie]))  # type: ignore[arg-type]
    monkeypatch.setattr(posixproc.os, "getpgid", _same_group)
    assert posixproc._group_empty(999) is True


def test_group_empty_false_with_live_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live member keeps the group occupied."""
    import types

    live = types.SimpleNamespace(pid=1)
    live.status = lambda: psutil.STATUS_RUNNING  # type: ignore[attr-defined]
    monkeypatch.setattr(posixproc.os, "killpg", _group_exists)
    monkeypatch.setattr(posixproc.psutil, "process_iter", lambda: iter([live]))  # type: ignore[arg-type]
    monkeypatch.setattr(posixproc.os, "getpgid", _same_group)
    assert posixproc._group_empty(999) is False
