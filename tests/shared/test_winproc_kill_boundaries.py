"""Tests for what a `shared.winproc` session kill is allowed to touch.

The defect these cover: on Windows nothing reparents a spawned process —
``DETACHED_PROCESS`` suppresses the console, it does not detach the parent link,
and there is no ``setsid`` / double fork / init to inherit orphans. So a session
a daemon spawns stays that daemon's child for its whole life, and
``proc.children(recursive=True)`` from one session reaches straight into another.
Because the hard kill goes children-first, the *spawned* session died before its
spawner did.

Measured on the fleet Windows box (2026-07-29): a gateway-triggered self-update
spawns the ``ava-updater`` session from inside the ops daemon; the updater's own
``ava restart`` stops this host's services, ``ava-ops`` among them; the kill of
``ava-ops``'s tree took the updater with it. ``ava-updater.out.log`` ends mid
teardown, one line after the watchdog, and the host was left both stopped and
un-updated. Agent processes sat in the same trap: ``ava cluster update``'s stop leaves
them running on purpose, yet stopping ``ava-ops`` force-killed every agent it had
launched, past the graceful path their ``finally`` needs.

Windows-only behaviour, asserted on any platform: the pruning is a pure decision
over a process tree, so the tree here is a fake and the assertions are about
which nodes the kill reached.

The second half of the file covers the other half of `kill_session`'s contract —
what it *reports*. `ok` means "confirmed gone", so a kill that does not take must
answer False and leave the session record alone (issue #1015). That branch was
unreachable while `_FakeProc.is_running` was `not self.killed`: no fake could be
a process that survives being killed, so reverting the confirmation to
unconditional success turned nothing red. `survives_polls` is what makes the
survivor expressible (issue #1121).
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import psutil
import pytest

from shared import winproc
from shared.session_record import SessionRecord

# Well above any real pid on the CI host, so a fake pid can never collide with
# the live ancestry `_self_ancestry_below` walks.
_PID_BASE = 900_000


@dataclass
class _FakeProc:
    """A node in a fake process tree, standing in for psutil.Process.

    `.kill()` is the *request*, not the outcome — `survives_polls` decides how
    many liveness polls the process keeps answering True for afterwards, so a
    kill that does not take is expressible. Everything downstream of the kill
    (the teardown wait, the confirmation, `has_session`) asks `is_running`, which
    is what makes a survivor visible to the code under test rather than only to
    the assertions.
    """

    pid: int
    kids: list[_FakeProc] = field(default_factory=list)
    killed: bool = False
    signals: list[int] = field(default_factory=list)
    # Liveness polls this process still answers True for *after* `.kill()`.
    # 0 = the kill takes at once (a normal process, and every tree node below
    # that is not deliberately made stubborn). A positive N = a straggler that
    # is gone by poll N+1. `math.inf` = the survivor a kill never reaches — the
    # process holding a port that no `terminate` reaches (a Windows service
    # wrapper, a hung driver I/O), which is the case `kill_session`'s
    # confirmation exists for and the one no fake could previously express.
    survives_polls: float = 0

    def children(self, recursive: bool = False) -> list[_FakeProc]:
        if not recursive:
            return list(self.kids)
        out: list[_FakeProc] = []
        for kid in self.kids:
            out.append(kid)
            out.extend(kid.children(recursive=True))
        return out

    def kill(self) -> None:
        self.killed = True

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)

    def is_running(self) -> bool:
        if not self.killed:
            return True
        if self.survives_polls <= 0:
            return False
        self.survives_polls -= 1  # `math.inf - 1` is `math.inf`: never dies
        return True


@dataclass
class _Fleet:
    """A fake tree plus the session records that name parts of it."""

    procs: dict[int, _FakeProc]
    waited: list[list[_FakeProc]]

    def proc(self, pid: int) -> _FakeProc:
        return self.procs[pid]

    def killed_pids(self) -> set[int]:
        return {p.pid for p in self.procs.values() if p.killed}


@pytest.fixture
def fleet(unit_home: Path, monkeypatch: pytest.MonkeyPatch) -> _Fleet:
    """The Windows agent-runner tree at the moment the updater stops the host.

        ava-ops (900000)                        <- the session being killed
        ├── ops-worker (900001)                 <- plain child, must die with it
        ├── ava-updater cmd.exe (900002)        <- its own session
        │   └── ava restart python (900003)     <- ...and that session's work
        └── ava-agent-42 (900004)               <- its own session

    Registers `ava-ops`, `ava-updater` and `ava-agent-42` as live session records
    and routes `_process_for_record` at the fake tree.
    """
    ops = _FakeProc(_PID_BASE)
    worker = _FakeProc(_PID_BASE + 1)
    updater = _FakeProc(_PID_BASE + 2)
    restart = _FakeProc(_PID_BASE + 3)
    agent = _FakeProc(_PID_BASE + 4)
    updater.kids = [restart]
    ops.kids = [worker, updater, agent]
    procs = {p.pid: p for p in (ops, worker, updater, restart, agent)}

    for name, pid in (
        ("ava-ops", ops.pid),
        ("ava-updater", updater.pid),
        ("ava-agent-42", agent.pid),
    ):
        SessionRecord(
            pid=pid, create_time=1.0, cmd=name, cwd=str(unit_home), started_at=time.time()
        ).write(winproc._record_path(name))

    def _for_record(rec: SessionRecord) -> _FakeProc | None:
        """The real helper answers None for a pid whose process is gone, and
        `has_session` is that answer. Routing the fake through `is_running` is
        what lets a survivor keep answering True to every consumer."""
        proc = procs.get(rec.pid)
        return proc if proc is not None and proc.is_running() else None

    monkeypatch.setattr(winproc, "_process_for_record", _for_record)
    waited: list[list[_FakeProc]] = []

    def _wait_procs(items: list[_FakeProc], timeout: float | None = None) -> tuple[list, list]:
        """One tick of `psutil.wait_procs`' poll loop.

        Real `wait_procs` re-checks each process until it is gone or `timeout`
        expires; polling once is the same partition with the wall clock removed,
        and it costs a `survives_polls` tick so a straggler can die *during* the
        teardown wait the way a real one does.
        """
        waited.append(list(items))
        gone, alive = [], []
        for p in items:
            (alive if p.is_running() else gone).append(p)  # pyright: ignore[reportUnknownMemberType]
        return gone, alive

    monkeypatch.setattr(psutil, "wait_procs", _wait_procs)  # pyright: ignore[reportUnknownArgumentType]

    # Signal transport has its own native Windows test; this fixture isolates
    # tree/spared-session waiting and force cleanup.
    def accept_signal(_name: str) -> bool:
        return True

    monkeypatch.setattr(winproc, "graceful_signal", accept_signal)
    return _Fleet(procs=procs, waited=waited)


def test_kill_session_with_verdict_live_session_is_interrupted(fleet: _Fleet) -> None:
    """The TTL reaper's interrupt verdict on Windows: the session process IS
    the work (new_session Popens the user's command directly — no shell
    layer), so killing any live session interrupted something; only the
    idempotent noop on an already-absent session reports not-interrupted."""
    ok, mode, interrupted = winproc.kill_session_with_verdict("ava-updater")
    assert (ok, mode, interrupted) == (True, "forced", True), (
        "a live session with a running child must report interrupted"
    )
    ok, mode, interrupted = winproc.kill_session_with_verdict("ava-agent-42")
    assert (ok, mode, interrupted) == (True, "forced", True), (
        "a live session WITHOUT children is still the work itself — busy"
    )
    ok, mode, interrupted = winproc.kill_session_with_verdict("ava-nonexistent")
    assert (ok, mode, interrupted) == (True, "noop", False), "an absent session was not interrupted"


def test_killing_a_daemon_spares_the_updater_session_it_spawned(fleet: _Fleet) -> None:
    """The regression: stopping `ava-ops` must not take down the `ava-updater`
    session, which on Windows is one of its children — the updater is the only
    thing that will run `ava start` afterwards."""
    ok, mode = winproc.kill_session("ava-ops", graceful=False)

    assert (ok, mode) == (True, "forced")
    assert not fleet.proc(_PID_BASE + 2).killed, "the ava-updater session was killed"
    assert not fleet.proc(_PID_BASE + 3).killed, "the updater's `ava restart` was killed"


def test_killing_a_daemon_spares_the_agent_processes_it_launched(fleet: _Fleet) -> None:
    """Agents are records in the same namespace and are also the ops daemon's
    children. `ava cluster update`'s stop leaves them running for the rollout to quiesce;
    a tree kill would force-kill them past their graceful shutdown."""
    winproc.kill_session("ava-ops", graceful=False)

    assert not fleet.proc(_PID_BASE + 4).killed


def test_tree_kill_would_spare_only_the_nested_session_subtree(
    fleet: _Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restart under the updater survives an ops-tree kill; a plain child does not."""
    monkeypatch.setattr(winproc, "IS_WINDOWS", True)
    ops = cast(psutil.Process, fleet.proc(_PID_BASE))

    assert winproc.tree_kill_would_spare("ava-ops", ops, {_PID_BASE + 2, _PID_BASE + 3})
    assert not winproc.tree_kill_would_spare("ava-ops", ops, {_PID_BASE + 1})


def test_the_targeted_session_and_its_plain_children_still_die(fleet: _Fleet) -> None:
    """Sparing is per-session, not a blanket retreat: the session asked for and
    every descendant that is not a session of its own is still killed."""
    winproc.kill_session("ava-ops", graceful=False)

    assert fleet.killed_pids() == {_PID_BASE, _PID_BASE + 1}
    assert not winproc._record_path("ava-ops").exists()


def test_an_unregistered_grandchild_is_still_reaped(
    fleet: _Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tree walk still recurses — `cmd -> uv -> python` under the target has
    no session record of its own and must not survive the stop."""
    uv = _FakeProc(_PID_BASE + 5)
    python = _FakeProc(_PID_BASE + 6)
    uv.kids = [python]
    fleet.proc(_PID_BASE).kids.append(uv)
    fleet.procs[uv.pid] = uv
    fleet.procs[python.pid] = python

    winproc.kill_session("ava-ops", graceful=False)

    assert uv.killed
    assert python.killed


def test_killing_a_session_does_not_kill_the_process_doing_the_killing(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second rule, independent of on-disk state: whoever runs the kill survives
    it even when its own session record is already gone (the stop unlinks records
    as it goes)."""
    target = _FakeProc(_PID_BASE)
    caller = _FakeProc(os.getpid())
    target.kids = [caller]
    procs = {target.pid: target, caller.pid: caller}

    SessionRecord(
        pid=target.pid, create_time=1.0, cmd="ava-ops", cwd=str(unit_home), started_at=time.time()
    ).write(winproc._record_path("ava-ops"))
    monkeypatch.setattr(winproc, "_process_for_record", lambda rec: procs.get(rec.pid))  # type: ignore[arg-type]
    monkeypatch.setattr(psutil, "wait_procs", lambda _items, timeout=None: ([], []))  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]

    winproc.kill_session("ava-ops", graceful=False)

    assert not caller.killed
    assert target.killed


def test_a_dead_session_record_spares_nothing(
    fleet: _Fleet, unit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale record whose process is gone must not hand its pid — which Windows
    recycles — to an unrelated live descendant as protection."""
    SessionRecord(
        pid=_PID_BASE + 1,  # same pid as the plain ops-worker child
        create_time=1.0,
        cmd="ava-stale",
        cwd=str(unit_home),
        started_at=time.time(),
    ).write(winproc._record_path("ava-stale"))
    # `_process_for_record` already answers None for a pid absent from the fake
    # tree; the stale record names a pid the tree *does* hold, so make the record
    # itself dead the way the real helper would (create_time mismatch).
    real = winproc._process_for_record
    monkeypatch.setattr(
        winproc,
        "_process_for_record",
        lambda rec: None if rec.cmd == "ava-stale" else real(rec),  # type: ignore[arg-type]
    )

    winproc.kill_session("ava-ops", graceful=False)

    assert fleet.proc(_PID_BASE + 1).killed


def test_the_graceful_wait_does_not_block_on_a_spared_session(
    fleet: _Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spared session is not in the graceful wait either — waiting on the
    updater to exit would burn the whole timeout on a process that is supposed to
    outlive this stop."""
    # Ctrl-Break exists only in the Windows `signal` module; the branch under test
    # is otherwise unreachable off-box.
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 1, raising=False)

    winproc.kill_session("ava-ops", graceful=True, timeout=0.01)

    assert fleet.waited, "the graceful path never waited"
    waited_pids = {p.pid for p in fleet.waited[0]}
    assert waited_pids == {_PID_BASE, _PID_BASE + 1}


# ── What a kill REPORTS when it does not take ───────────────────────────────
#
# `ok` is the SessionBackend contract's "confirmed gone". The caller's response
# to a successful stop is to launch the service again, so a kill that reports a
# success it did not achieve hands the next start a port that is still held —
# `[Errno 48] Address already in use`, from a stop that printed a check mark
# (issue #1015). Below, `ava-ops` is a process the kill never reaches.


@pytest.fixture
def survivor(fleet: _Fleet) -> _FakeProc:
    """`ava-ops` as a kill that does not take: still running, still holding its
    port, after the tree walk and the teardown wait are both done with it.

    Only the session's own process is stubborn — its plain child dies normally,
    so the tree walk is exactly the one the tests above pin and what changes is
    only what the kill is entitled to report about it.
    """
    ops = fleet.proc(_PID_BASE)
    ops.survives_polls = math.inf
    return ops


def test_a_force_kill_that_does_not_take_reports_failure(
    fleet: _Fleet, survivor: _FakeProc
) -> None:
    """The kill was issued and refused. `ok` is not "we asked" — it is "it is
    gone" — so the only honest answer is False."""
    ok, mode = winproc.kill_session("ava-ops", graceful=False)

    assert survivor.killed, "the kill was never even attempted"
    assert (ok, mode) == (False, "forced")


def test_a_graceful_kill_that_does_not_take_reports_failure(
    fleet: _Fleet, survivor: _FakeProc, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same for the graceful path, which is the one `ava cluster update` takes: Ctrl-Break
    went out, the wait expired, the hard kill went out, and the process is still
    there. `mode` still names the path taken — it reports how, `ok` reports
    whether."""
    monkeypatch.setattr(signal, "CTRL_BREAK_EVENT", 1, raising=False)

    ok, mode = winproc.kill_session("ava-ops", graceful=True, timeout=0.01)

    assert (ok, mode) == (False, "graceful")


def test_the_survivors_session_record_is_kept(fleet: _Fleet, survivor: _FakeProc) -> None:
    """The record is the only handle anything on this host has on that pid.
    Unlinking it while the process runs does not stop the process — it just
    removes it from view, which is strictly worse than the stop failing."""
    winproc.kill_session("ava-ops", graceful=False)

    assert winproc._record_path("ava-ops").exists()


def test_has_session_stays_true_while_the_process_survives(
    fleet: _Fleet, survivor: _FakeProc
) -> None:
    """The consumer that decides whether to launch. A False here is the whole
    defect: the service is running, nothing knows it, and the next start binds
    the port it already holds."""
    winproc.kill_session("ava-ops", graceful=False)

    assert winproc.has_session("ava-ops")


def test_a_survivor_stays_in_the_session_listing(fleet: _Fleet, survivor: _FakeProc) -> None:
    """`list_sessions` reaps records whose process is gone — this one's is not,
    so an operator (and `ava status`) still sees what is holding the port."""
    winproc.kill_session("ava-ops", graceful=False)

    assert "ava-ops" in winproc.list_sessions("ava-")


def test_the_caller_does_not_relaunch_into_the_held_port(
    fleet: _Fleet, survivor: _FakeProc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the kill-then-launch pair every respawn is made of
    (`shared.service_respawn.respawn_service` on the native backend): the failed
    kill leaves the session visible, so `new_session`'s idempotence guard holds
    and no second process is started against a port the first still owns.
    """
    launched: list[str | list[str]] = []

    class _RecordingPopen:
        def __init__(self, command: str | list[str], **_: object) -> None:
            launched.append(command)
            self.pid = os.getpid()

    monkeypatch.setattr(subprocess, "Popen", _RecordingPopen)

    winproc.kill_session("ava-ops", graceful=False)
    winproc.new_session("ava-ops", ".venv/bin/python -m services.ops.daemon", tmp_path, env={})

    assert launched == [], f"relaunched into the port pid {survivor.pid} still holds: {launched}"


def test_the_survivor_is_named_in_the_log(
    fleet: _Fleet,
    survivor: _FakeProc,
    loguru_records: list[dict],
) -> None:
    """A failed stop that is only a False in a return value is a failed stop
    nobody reads. The pid is the operator's only lead on what to kill by hand."""
    winproc.kill_session("ava-ops", graceful=False)

    errors = [r for r in loguru_records if r["level"].name == "ERROR"]  # pyright: ignore[reportUnknownMemberType]
    assert any(str(survivor.pid) in r["message"] for r in errors), errors


def test_a_straggler_that_dies_during_the_teardown_wait_is_confirmed(fleet: _Fleet) -> None:
    """The other side of the same branch, so the confirmation cannot degenerate
    into "a forced kill always failed": a process that is still up when the kill
    lands but gone by the time the teardown wait is done IS confirmed, and its
    record is reaped like any other clean stop."""
    fleet.proc(_PID_BASE).survives_polls = 1

    ok, mode = winproc.kill_session("ava-ops", graceful=False)

    assert (ok, mode) == (True, "forced")
    assert not winproc._record_path("ava-ops").exists()
