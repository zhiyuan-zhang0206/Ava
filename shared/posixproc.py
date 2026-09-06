"""POSIX process supervisor — the native host for detached agent processes.

An agent process is non-interactive: it talks to the world over DB + Redis and
logs to a file, so a host session for it no longer costs a PTY (macOS
caps `kern.tty.ptmx_max`, a whole-box wall the fleet hit long before any real
resource limit). This module hosts agents the way `shared.winproc` hosts them on
Windows — a named, detached process tracked by a small on-disk record — so the
POSIX agent-launch path is fully native. Daemons run here too (services
need no PTY, and the per-box PTY ceiling then stops bounding service count);
only the agents' own persistent shells live elsewhere, each in its own
detached pty host (`shared/pty_sessions`), which keeps the long-lived
interactive pane.

The surface mirrors `shared.winproc` one-to-one so `ops.agent_launch` and the
reap / status consumers dispatch to one of the two by platform:

- a "session" is a named process launched **double-forked** (via
  `shared._reparent`) so it reparents to init immediately — a long-lived spawner
  (gateway / ops daemon) never accumulates it as a zombie, and no PTY is
  allocated;
- its identity (pid + start-time, to defeat pid recycling) is recorded as JSON
  under `$AVA_HOME/run/sessions/<name>.json`;
- stdout/stderr are redirected to `$AVA_HOME/logs/<name>.out.log` (+ an optional
  split stderr file) so a crash's last words survive the process ending;
- liveness = the record's pid is alive *and* its start-time matches the record;
- kill signals the process's whole group (graceful SIGTERM → the agent's finally
  runs; force SIGKILL) — a group signal also reaches children spawned mid-kill.

Agent liveness of record is `agents_meta.pid` in the DB — the respawn controller
reaps by that pid, and force-terminate kills by it. This record layer is the
*no-DB* fallback: `ava stop` tears the gateway + DB down, then reaps the agents
it left running by enumerating these records.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from shared.log import logger
from shared.paths import logs_dir, run_dir
from shared.platform import CREATE_NO_WINDOW
from shared.session_record import SessionRecord, pid_starttime_ticks

# psutil exceptions that mean "the process is already gone / not ours to touch" —
# benign during a teardown race.
_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, OSError)

# A pid is "the same process we launched" only if its start-time matches to
# within this tolerance — guards against the OS recycling the pid onto an
# unrelated process after ours exits.
_CREATE_TIME_TOLERANCE_S = 2.0

# create_time stamped on a session whose child died before the record was
# written. No real process start time is negative, so `_process_for_record`
# (|proc.create_time() - rec.create_time| > tolerance) can never match it —
# the record reads as dead from birth instead of claiming the pid's next
# occupant (see new_session).
_DEAD_CHILD_SENTINEL = -1.0

# The reparent helper double-forks + execs in microseconds; it must never hang
# the spawner. A generous ceiling that only trips on a genuinely wedged fork.
_SPAWN_HELPER_TIMEOUT_S = 30.0

# Poll interval for the graceful group-empty wait in _terminate_tree.
# (Name avoids the clock-lattice vocabulary; this is a polling cadence, not a clock.)
_KILL_POLL_S = 0.05


def _sessions_dir() -> Path:
    d = run_dir() / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _record_path(name: str) -> Path:
    return _sessions_dir() / f"{name}.json"


def session_log_path(name: str) -> Path:
    """The combined stdout+stderr log for a session ($AVA_HOME/logs/<name>.out.log)."""
    return logs_dir() / f"{name}.out.log"


def _read_record(name: str) -> SessionRecord | None:
    return SessionRecord.read(_record_path(name))


def _process_is_live(proc: psutil.Process) -> bool:
    """True when `proc` is genuinely running — a zombie counts as dead.

    `is_running()` alone is zombie-blind: a zombie's status is not DEAD, so a
    kill whose victim init has not reaped yet looks like a survivor. Same
    semantics as the orphan-reaper fix (#1272): a corpse awaiting its reaper
    cannot run again, so it is not a live session.
    """
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _process_for_record(rec: SessionRecord) -> psutil.Process | None:
    """Return the live psutil.Process named by `rec`, or None if it is gone or a
    different (recycled) process now holds the pid."""
    try:
        proc = psutil.Process(rec.pid)
        if not _process_is_live(proc):
            return None
        if rec.starttime is not None:
            return proc if rec.identifies(rec.pid) is True else None
        # create_time defeats pid recycling for legacy records and platforms
        # without Linux's clock-stable `/proc/<pid>/stat` field 22.
        if abs(proc.create_time() - rec.create_time) > _CREATE_TIME_TOLERANCE_S:
            return None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    return proc


def has_session(name: str) -> bool:
    """True if the named session's process is still alive."""
    rec = _read_record(name)
    if rec is None:
        return False
    return _process_for_record(rec) is not None


def new_session(
    name: str,
    cmd: str | list[str],
    cwd: Path,
    *,
    env: dict[str, str],
    stderr_append: Path | None = None,
) -> bool:
    """Launch `cmd` as a detached, named background session.

    `cmd` is either a shell command line (`str`) run through `sh -c` or an
    already-split argv (`list[str]`) execed directly with no shell — the agent
    launcher passes argv so nothing it composes (an interpreter path with a
    space, the agent id) is ever re-parsed by a shell.
    `env` is the full child environment (the caller builds it). stdout+stderr go
    to the session log; `stderr_append`, when given, splits stderr to that file
    (the agent stderr log).

    The child is double-forked via `shared._reparent` so it reparents to init and
    the spawner accretes no zombie. An existing live session of the same name is
    left untouched (idempotent), matching the has-session guard at the call site.

    Returns True on a successful spawn.

    Raises:
        RuntimeError: the reparent helper failed to launch / report a pid.
    """
    if has_session(name):
        return True
    argv = ["/bin/sh", "-c", cmd] if isinstance(cmd, str) else list(cmd)
    stdout_path = session_log_path(name)
    stderr_path = stderr_append if stderr_append is not None else stdout_path
    # `2>>{path}` / the helper's os.open do not mkdir; guarantee the dir on a
    # fresh machine so the launch cannot fail on a missing $AVA_HOME/logs.
    logs_dir().mkdir(parents=True, exist_ok=True)

    # Run the reparent helper and wait for it (it exits in microseconds after
    # forking the real child). Waiting reaps the helper — the spawner's ONLY
    # direct child — so no zombie is left; the real child is already reparented
    # to init. The helper prints the child's pid to stdout.
    helper = [sys.executable, "-m", "shared._reparent", str(stdout_path), str(stderr_path), *argv]
    result = subprocess.run(  # noqa: S603 — argv composed from repo-internal literals + int agent_id
        helper,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
        text=True,
        timeout=_SPAWN_HELPER_TIMEOUT_S,
        check=False,
    )
    child_pid_str = result.stdout.strip()
    if result.returncode != 0 or not child_pid_str.isdigit():
        raise RuntimeError(
            f"reparent helper failed to launch session {name!r} "
            f"(exit {result.returncode}): stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    child_pid = int(child_pid_str)

    try:
        create_time = psutil.Process(child_pid).create_time()
    except psutil.NoSuchProcess:
        # The child already exited (instant failure). Record a sentinel that
        # can never match a real process start time: a pid freed by an
        # instantly-dead child is at its most reusable, and recording
        # time.time() would make `_process_for_record`'s create-time check
        # accept the pid's next innocent occupant — a later kill_session
        # could then SIGKILL an unrelated process tree (audit 2026-08-08 P2).
        # The record still forms (the caller's readiness probe sees it die).
        create_time = _DEAD_CHILD_SENTINEL
        starttime = None
    else:
        starttime = pid_starttime_ticks(child_pid)
    SessionRecord(
        pid=child_pid,
        create_time=create_time,
        cmd=cmd if isinstance(cmd, str) else " ".join(cmd),
        cwd=str(cwd),
        started_at=time.time(),
        starttime=starttime,
    ).write(_record_path(name))
    return True


def graceful_signal(name: str, *, expected: SessionRecord | None = None) -> bool:
    """Send the graceful-stop signal (SIGTERM) to the session's process without
    waiting or force-killing.

    The agent converts SIGTERM to SystemExit and runs its finally (closing the
    MCP daemon + Claude Code subprocess it tracks). Returns True if a live
    process was signaled. `ava stop`'s reap uses this to SIGTERM every agent, then
    wait on all of them under one shared deadline before force-killing stragglers
    — so the teardown is O(slowest agent), not O(sum of agents).
    """
    rec = _read_record(name)
    if rec is None or (expected is not None and rec != expected):
        return False
    proc = _process_for_record(rec)
    if proc is None:
        return False
    with contextlib.suppress(*_GONE):
        if expected is not None:
            if _read_record(name) != expected:
                return False
            if expected.starttime is not None:
                # WSL btime can move the epoch birth of this same process. The
                # recorded /proc ticks are authoritative, including this final
                # check; unavailable or changed ticks must still refuse delivery.
                if expected.identifies(proc.pid) is not True:
                    return False
            elif proc.create_time() != expected.create_time:
                return False
        # Signal only this captured process object, never resolve the name a
        # second time into a replacement target. psutil also guards PID reuse.
        proc.terminate()  # SIGTERM to the agent only; its finally closes its children
        return True
    return False


def _pgid_of(proc: psutil.Process) -> int | None:
    """The process group the session process lives in, or None when it is gone.

    ``shared._reparent`` setsid()s the helper before forking, so the launched
    process — and every descendant that does not deliberately leave the group —
    shares one pgid (the helper's pid; the helper itself exits at once).
    psutil has no pgid accessor, so this reads it via ``os.getpgid``."""
    with contextlib.suppress(OSError):
        return os.getpgid(proc.pid)
    return None


def _signal_group(pgid: int | None, sig: int) -> None:
    """Send `sig` to every process in the group, or nothing when it is unknown.

    A group signal reaches children spawned after any psutil snapshot — the
    hard-kill window the old children-list walk missed (task #2249). A dead
    leader frees its pid for reuse; a reused pid that setsid()s into a new
    group could then be hit here (µs window, same shape as pty_sessions/PITR
    group kills — accepted)."""
    if pgid is None or pgid <= 0:
        return
    with contextlib.suppress(OSError):
        os.killpg(pgid, sig)


def _group_empty(pgid: int | None) -> bool:
    """True when no LIVE process remains in the group (identity unknown → True).

    Fast path: a gone group is empty. A surviving group is checked member by
    member: a zombie still occupies the group (and makes ``os.killpg(pgid, 0)``
    succeed) until init/launchd reaps it, and the graceful verdict must not
    wait on that reap latency — same class as the zombie-aware liveness of
    task #1303."""
    if pgid is None or pgid <= 0:
        return True
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False  # exists but un-signallable — treat as occupied
    for process in psutil.process_iter():
        try:
            if os.getpgid(process.pid) == pgid and process.status() != psutil.STATUS_ZOMBIE:
                return False
        except (psutil.Error, ProcessLookupError):
            continue
    return True


def _terminate_tree(proc: psutil.Process, *, graceful: bool, timeout: float) -> bool:
    """Terminate `proc` and its whole process group.

    The session process runs in its own process group (``shared._reparent``
    setsid()s the helper; the launched process and every descendant that does
    not deliberately leave the group share that pgid). Signaling the GROUP —
    SIGTERM to all members, wait, SIGKILL to all members — closes the window
    the old psutil-only walk left open: a child spawned DURING the graceful
    wait (a bash wrapper's foreground command, a process an unwound finally
    spawns) was not in the pre-captured children list, so the hard-kill walk
    missed it and it survived as an orphan (task #2249).

    graceful: first SIGTERM **the whole group** and wait up to `timeout` for
    it to empty — the process converts SIGTERM to a
    KeyboardInterrupt/SystemExit unwind and runs its finally (an agent closes
    the MCP daemon + Claude Code subprocess it tracks; a service daemon closes
    its pidfile and pools), and its children die with it. Whatever is still
    alive after the wait is hard-killed.
    force: SIGKILL the whole group immediately.

    Returns True when the graceful signal alone emptied the group AND every
    pre-existing descendant — the caller reports that as the `mode`, so an
    escalation to SIGKILL cannot be mislabelled a clean stop.
    """
    # Snapshot BEFORE the graceful signal: a descendant that deliberately left
    # the group (a setsid'd worker) is unreachable by group signal, and once
    # the leader dies it reparents to init — this walk is the only handle on
    # it, so the graceful verdict checks it and the hard kill reaps it. An
    # already-gone leader yields an empty list.
    children: list[psutil.Process] = []
    with contextlib.suppress(*_GONE):
        children = proc.children(recursive=True)
    pgid = _pgid_of(proc)
    if graceful:
        # SIGTERM the whole group, not just the top process: a bash wrapper
        # delays its own SIGTERM until its foreground command ends, and a
        # command an unwound finally spawns mid-wait is not in any children
        # snapshot — only a group signal reaches both.
        _signal_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (
                not _process_is_live(proc)
                and _group_empty(pgid)
                and all(not _process_is_live(c) for c in children)
            ):
                return True
            time.sleep(_KILL_POLL_S)
    # Hard kill: the group first (a SIGTERM-ignoring straggler or a child
    # spawned during the graceful wait dies here), then the pre-captured walk
    # for a descendant that left the group (a setsid'd worker) — the group
    # signal cannot reach it, and once the leader dies it reparents to init.
    _signal_group(pgid, signal.SIGKILL)
    for child in [*children, proc]:
        with contextlib.suppress(*_GONE):
            child.kill()
    with contextlib.suppress(*_GONE):
        psutil.wait_procs([proc, *children], timeout=5)
    return False


def kill_session(name: str, *, graceful: bool = False, timeout: float = 15.0) -> tuple[bool, str]:
    """Stop the named session and forget it (force / graceful).

    Returns (ok, mode) with mode in {graceful, forced, noop}. `mode` reports what
    actually happened, not what was asked for: a graceful stop the process
    ignored until the SIGKILL fallback comes back `forced`, so the caller's
    escalation marker fires instead of the clean-stop one hiding a hard kill.

    Idempotent: an absent/dead session is a noop.
    """
    rec = _read_record(name)
    if rec is None:
        return True, "noop"
    proc = _process_for_record(rec)
    if proc is None:
        _record_path(name).unlink(missing_ok=True)
        return True, "noop"
    ended_on_signal = False
    try:
        ended_on_signal = _terminate_tree(proc, graceful=graceful, timeout=timeout)
    except Exception as exc:
        logger.warning("posixproc kill {name} hit {exc}", name=name, exc=exc)
    mode = "graceful" if ended_on_signal else "forced"
    # Kill result re-check (mirrors winproc's #1015 fix): a kill that reports
    # success it did not achieve turns a live-but-unbacked session into a
    # service nothing starts. The contract (session_backend.py) says `ok`
    # means the session is confirmed gone; on a survivor we keep the record —
    # the no-DB reap's only view of the process — and say so. A zombie is not
    # a survivor: it is dead, just awaiting its parent's reap.
    if _process_is_live(proc):
        logger.error(
            "posixproc kill {name}: pid {pid} is still running after the kill — leaving "
            "its session record in place so the survivor stays visible",
            name=name,
            pid=proc.pid,
        )
        return False, mode
    _record_path(name).unlink(missing_ok=True)
    return True, mode


def session_started_at(name: str) -> float | None:
    """Epoch seconds when the named session was launched, or None when it is
    not alive (no record, or the record's process is gone).

    Read off the on-disk record — the same source has_session / list_sessions
    judge — so a liveness consumer can render an uptime without a second probe.
    """
    rec = _read_record(name)
    if rec is None:
        return None
    if _process_for_record(rec) is None:
        return None
    return rec.started_at


def _record_reapable(rec: SessionRecord) -> tuple[bool, str]:
    """Whether a failed liveness check proves a record can be discarded."""
    if _process_for_record(rec) is not None:
        return False, "process is still live"
    try:
        proc = psutil.Process(rec.pid)
        if not proc.is_running() or not _process_is_live(proc):
            return True, "pid is no longer running"
    except psutil.NoSuchProcess:
        return True, "pid is gone"
    except (psutil.AccessDenied, OSError):
        return False, "pid could not be inspected"
    if rec.identifies(rec.pid) is False:
        return True, "pid was reused by another process"
    return False, "live pid did not satisfy the legacy identity check"


def list_sessions(prefix: str = "") -> list[str]:
    """Names of all live sessions, optionally filtered by `prefix`.

    Reaps records whose process is gone so the listing reflects reality.
    """
    out: list[str] = []
    for rec_file in _sessions_dir().glob("*.json"):
        name = rec_file.stem
        if prefix and not name.startswith(prefix):
            continue
        rec = SessionRecord.read(rec_file)
        if rec is None:
            rec_file.unlink(missing_ok=True)
        elif _process_for_record(rec) is not None:
            out.append(name)
        else:
            reapable, why = _record_reapable(rec)
            if not reapable:
                logger.warning(
                    "posixproc retaining live session record {name}: {why}",
                    name=name,
                    why=why,
                )
                continue
            rec_file.unlink(missing_ok=True)
    return out
