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
- kill signals the process (graceful SIGTERM → the agent's finally runs) or its
  whole tree (force SIGKILL).

Agent liveness of record is `agents_meta.pid` in the DB — the respawn controller
reaps by that pid, and force-terminate kills by it. This record layer is the
*no-DB* fallback: `ava stop` tears the gateway + DB down, then reaps the agents
it left running by enumerating these records.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import time
from pathlib import Path

import psutil

from shared.log import logger
from shared.paths import logs_dir, run_dir
from shared.platform import CREATE_NO_WINDOW
from shared.session_record import SessionRecord

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


def _process_for_record(rec: SessionRecord) -> psutil.Process | None:
    """Return the live psutil.Process named by `rec`, or None if it is gone or a
    different (recycled) process now holds the pid."""
    try:
        proc = psutil.Process(rec.pid)
        # create_time defeats pid recycling: a new process on the same pid has a
        # later start time than the one we recorded.
        if abs(proc.create_time() - rec.create_time) > _CREATE_TIME_TOLERANCE_S:
            return None
        if not proc.is_running():
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
    SessionRecord(
        pid=child_pid,
        create_time=create_time,
        cmd=cmd if isinstance(cmd, str) else " ".join(cmd),
        cwd=str(cwd),
        started_at=time.time(),
    ).write(_record_path(name))
    return True


def graceful_signal(name: str) -> bool:
    """Send the graceful-stop signal (SIGTERM) to the session's process without
    waiting or force-killing.

    The agent converts SIGTERM to SystemExit and runs its finally (closing the
    MCP daemon + Claude Code subprocess it tracks). Returns True if a live
    process was signaled. `ava stop`'s reap uses this to SIGTERM every agent, then
    wait on all of them under one shared deadline before force-killing stragglers
    — so the teardown is O(slowest agent), not O(sum of agents).
    """
    rec = _read_record(name)
    if rec is None:
        return False
    proc = _process_for_record(rec)
    if proc is None:
        return False
    with contextlib.suppress(*_GONE):
        proc.terminate()  # SIGTERM to the agent only; its finally closes its children
        return True
    return False


def _terminate_tree(proc: psutil.Process, *, graceful: bool, timeout: float) -> bool:
    """Terminate `proc` and, if it does not go on its own, its whole tree.

    graceful: first SIGTERM **only the top process** and wait up to `timeout` —
    the process converts SIGTERM to a `KeyboardInterrupt`/`SystemExit` unwind and
    runs its finally (an agent closes the MCP daemon + Claude Code subprocess it
    tracks; a service daemon closes its pidfile and pools), then exits, taking
    its own children with it. Whatever is still alive after the wait is
    hard-killed.
    force: SIGKILL the whole tree immediately.

    Returns True when the graceful signal alone ended the tree — the caller
    reports that as the `mode`, so an escalation to SIGKILL cannot be
    mislabelled a clean stop.
    """
    children = proc.children(recursive=True)
    if graceful:
        with contextlib.suppress(*_GONE):
            proc.terminate()  # SIGTERM → the process's lifecycle handler runs finally
        _gone, alive = psutil.wait_procs([proc, *children], timeout=timeout)
        if not alive:
            return True
    # Hard kill: children first so a parent can't respawn a child mid-teardown.
    for p in [*children, proc]:
        with contextlib.suppress(*_GONE):
            p.kill()  # SIGKILL
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
    # the no-DB reap's only view of the process — and say so.
    if proc.is_running():
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


def list_sessions(prefix: str = "") -> list[str]:
    """Names of all live sessions, optionally filtered by `prefix`.

    Reaps records whose process is gone so the listing reflects reality.
    """
    out: list[str] = []
    for rec_file in _sessions_dir().glob("*.json"):
        name = rec_file.stem
        if prefix and not name.startswith(prefix):
            continue
        if has_session(name):
            out.append(name)
        else:
            rec_file.unlink(missing_ok=True)
    return out
