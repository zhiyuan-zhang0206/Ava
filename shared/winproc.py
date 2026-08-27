"""Windows process supervisor — the native-Windows session surface.
detached-session model (issue: Phase-2 native Windows agent-runner).

The session surface gives Ava three things: a *named* handle to a long-running
detached process, a liveness check by that name, and a kill by that name. There
is no POSIX process supervisor on Windows, so this module reproduces that surface with plain Win32
process primitives:

- a "session" is a named, detached child process that survives the parent
  ``ava start`` exiting and owns its own process group (for Ctrl-Break);
  ``_plan_launch`` decides whether cmd.exe is in the middle (see below);
- its identity (pid + process start-time, to defeat pid recycling) is recorded
  as JSON under ``$AVA_HOME/run/sessions/<name>.json``;
- stdout/stderr are redirected to ``$AVA_HOME/logs/<name>.out.log`` (the session
  pane analog) so a crash's last words survive the session ending;
- liveness = the record's pid is alive *and* its start-time matches the record;
- kill walks the process tree (cmd -> uv -> python, npm -> node, …) and
  terminates it, graceful-first via Ctrl-Break when asked — but the walk stops at
  another session's process, see `_spared_pids`.

The public surface mirrors the POSIX session helpers in ``cli/commands/_session_lifecycle.py`` so the
dispatch there is a thin ``if IS_WINDOWS`` fork. This module is import-safe on
every platform (it only touches ``ctypes``/``psutil`` lazily inside Windows
branches), but its functions are only ever *called* on Windows.

**Why the launch is not simply ``cmd /c``.** cmd.exe started with
``DETACHED_PROCESS`` finds itself without a console and allocates one, then hands
*its children* that console's handles — overriding both the log-file handles it
inherited and any ``>>`` redirect it was asked to perform. Measured on the fleet
Windows box: a child under ``cmd /c`` gets stdout/stderr of ``FILE_TYPE_CHAR``
(the throwaway console), the same child launched directly gets ``FILE_TYPE_DISK``
(the log). So a daemon that dies at startup under ``cmd /c`` leaves NO trace on
disk — its last words go to a console nobody is attached to, which dies with it.
``_plan_launch`` is the fix: a command that needs no shell is launched as a plain
argv with no cmd.exe in the middle, and a command that genuinely needs cmd (a
``&&`` chain, ``set VAR=val``) gets ``CREATE_NO_WINDOW`` instead of
``DETACHED_PROCESS`` so cmd starts *with* a console and never allocates one,
leaving the inherited log-file handles in place for its children.

**Why a kill stops at session boundaries.** Neither ``DETACHED_PROCESS`` nor
``CREATE_NEW_PROCESS_GROUP`` reparents a child on Windows — there is no
``setsid``, no double fork, and no init to inherit orphans, so a session spawned
*by a daemon* stays that daemon's child in the process table for its whole life.
A daemon on this box is therefore an ancestor of everything it ever started, and
a naive ``children(recursive=True)`` tree kill of one session reaches straight
into unrelated ones. POSIX gets that isolation from the launch side (the
supervisor owns the children; ``shared.posixproc`` double-forks agents to init), so
the same invariant is reproduced here from the *kill* side by `_spared_pids`.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from shared.log import logger
from shared.paths import logs_dir, run_dir
from shared.platform import IS_WINDOWS
from shared.session_record import SessionRecord
from shared.winjob import in_attached_exec_job

# psutil exceptions that mean "the process is already gone / not ours to touch" —
# benign during a teardown race.
_GONE = (psutil.NoSuchProcess, psutil.AccessDenied, OSError)

# Win32 CREATE_* flags. subprocess re-exports them on Windows only, so the
# fallback is the ABI constant rather than 0: the launch plan then carries the
# same values on every platform and can be asserted off-box. (A Windows-only
# defect that is only ever unit-tested on macOS is how this class of bug
# survives — a 0 fallback makes both branches indistinguishable in a test.)
#
# Own process group, so we can deliver Ctrl-Break to it (and only it).
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
# A persistent session spawned by a one-shot exec must leave that exec's Job
# Object. The flag is added only in the explicit exec-job context; outside it,
# an unrelated outer Job may forbid breakaway and reject the launch.
_BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
# No console at all — correct for a process we launch ourselves and whose
# stdout/stderr handles we own.
_DETACHED_FLAGS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | _NEW_GROUP
# A console that has no window. Used only when cmd.exe is the child: cmd
# allocates a console when it has none and then gives its own children that
# console's handles, swallowing everything they print (see the module docstring).
# Handing it a windowless console up front stops the allocation, so the log-file
# handles we passed reach the commands cmd runs. Verified to survive the
# launching process — and the ssh session that started it — exiting.
_CMD_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | _NEW_GROUP

# cmd.exe operators that give a command line a shape CreateProcess cannot
# reproduce on its own: `&&` / `||` / `&` chaining, pipes, redirects, `%VAR%`
# expansion, `^` escapes. A command string free of every one of them is just an
# argv wearing a string, and goes out without a shell.
_CMD_OPERATORS = frozenset("&|<>^%")

# The POSIX interpreter path the service specs are written with. It names the
# checkout's venv, not a file that exists on Windows.
_POSIX_VENV_PYTHON = ".venv/bin/python"

# A pid is "the same process we launched" only if its start-time matches to
# within this tolerance — guards against Windows recycling the pid onto an
# unrelated process after ours exits.
_CREATE_TIME_TOLERANCE_S = 2.0

# create_time stamped on a session whose child died before the record was
# written. No real process start time is negative, so `_process_for_record`
# can never match it — the record reads as dead from birth instead of
# claiming the pid's next occupant (see new_session).
_DEAD_CHILD_SENTINEL = -1.0


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


# ── Launch shape ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Launch:
    """How a session command is handed to CreateProcess.

    `command` is either an argv (launched directly, no shell) or a fully formed
    command line string (``cmd /s /c "…"``, passed verbatim so no quoting layer
    sits between the caller's cmd.exe syntax and cmd.exe). `creationflags` follows
    from that choice — see `_DETACHED_FLAGS` / `_CMD_FLAGS`.
    """

    command: str | list[str]
    creationflags: int

    @property
    def via_shell(self) -> bool:
        return isinstance(self.command, str)

    @property
    def display(self) -> str:
        """The launch as one line, for the session record's provenance field."""
        return self.command if isinstance(self.command, str) else " ".join(self.command)


def _windows_python(cwd: Path) -> str:
    """The interpreter `.venv/bin/python` names on Windows — the checkout's venv
    if it has one, else whatever `python` resolves to on PATH."""
    venv_python = cwd / ".venv" / "Scripts" / "python.exe"
    return str(venv_python) if venv_python.exists() else "python"


def _split_argv(cmd: str) -> list[str] | None:
    """Split a shell-free command string into an argv, or None if it cannot be
    split unambiguously.

    Windows quoting, not POSIX: only `"` groups (a lone `'` is a literal), and a
    backslash is a path separator rather than an escape. An unbalanced quote
    returns None so the caller falls back to cmd.exe rather than guessing.
    """
    lexer = shlex.shlex(cmd, posix=False)
    lexer.whitespace_split = True
    lexer.quotes = '"'
    lexer.escape = ""
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    return [t[1:-1] if len(t) >= 2 and t.startswith('"') and t.endswith('"') else t for t in tokens]


def _plan_launch(cmd: str | list[str], cwd: Path) -> _Launch:
    """Decide how `cmd` reaches CreateProcess — directly, or through cmd.exe.

    The branch is the whole point of this module's Windows-specific care: cmd.exe
    swallows its children's stdout/stderr under `DETACHED_PROCESS` (module
    docstring), so the shell is used only where the command's own syntax demands
    it, and where it is used cmd is given a console it will not replace.
    """
    if not isinstance(cmd, str):
        # Already an argv (the agent launcher, whose composed elements —
        # interpreter path, agent id — must not meet a shell at all). The
        # interpreter rewrite applies here too: a caller composing the POSIX
        # `.venv/bin/python` token must get the same Windows resolution as a
        # string command, or the element reaches CreateProcess as a path that
        # cannot exist (the '.venv' is not recognized class — see the shell
        # branch's history). Absolute interpreter paths pass through untouched.
        python = _windows_python(cwd)
        return _Launch([python if t == _POSIX_VENV_PYTHON else t for t in cmd], _DETACHED_FLAGS)
    argv = None if _CMD_OPERATORS & set(cmd) else _split_argv(cmd)
    if argv is not None:
        python = _windows_python(cwd)
        return _Launch([python if t == _POSIX_VENV_PYTHON else t for t in argv], _DETACHED_FLAGS)
    # Shell semantics are genuinely wanted. `/s` makes cmd strip exactly the
    # outer quote pair and take the rest literally — the one documented way to
    # pass a command line containing its own quotes (`set "VAR=val" && …`)
    # through untouched. Popen is handed the string, not a list: list2cmdline
    # would escape those inner quotes as \" , which cmd.exe reads as a literal
    # backslash plus a quote toggle.
    python = _windows_python(cwd)
    shell_cmd = cmd.replace(_POSIX_VENV_PYTHON, f'"{python}"' if " " in python else python)
    return _Launch(f'cmd /s /c "{shell_cmd}"', _CMD_FLAGS)


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


def session_has_active_processes(name: str) -> bool:
    """True when the session carries live descendants beyond its own process.

    The TTL reaper's interrupt probe: a session whose recorded process has
    children is running work inside it (the session process itself is the
    idle shell / cmd, not "work"). A session that cannot be inspected answers
    True — fail-open, a session that cannot be proven idle may be running
    something."""
    rec = _read_record(name)
    if rec is None:
        return False  # session absent — nothing to interrupt
    proc = _process_for_record(rec)
    if proc is None:
        return False  # session process gone — nothing to interrupt
    try:
        return bool(proc.children(recursive=True))
    except psutil.Error:
        return True  # cannot inspect — assume the worst


def new_session(
    name: str,
    cmd: str | list[str],
    cwd: Path,
    *,
    env: dict[str, str],
    stderr_append: Path | None = None,
) -> bool:
    """Launch `cmd` as a detached, named background session.

    `cmd` is either a command line (``str``) or an already-split argv
    (``list[str]``, which the agent launcher uses so its composed elements never
    meet cmd's quoting hazards). A string that needs no
    shell is split and launched directly; one that uses cmd.exe syntax (``&&``
    chains, ``set VAR=val``) runs through cmd.exe — see `_plan_launch`, which
    also settles the creation flags that decide whether the command's output
    survives. `env` is the full child environment (the caller passes
    ``os.environ`` + its overrides). stdout and stderr go to the session log; if
    `stderr_append` is given, stderr is split out to that file (the agent stderr
    log).

    Returns True on a successful spawn. An existing live session of the same
    name is left untouched (idempotent), matching the has-session guard at the call
    site.
    """
    if has_session(name):
        return True
    launch = _plan_launch(cmd, cwd)
    creationflags = launch.creationflags
    if in_attached_exec_job():
        creationflags |= _BREAKAWAY
    out = session_log_path(name).open("ab")
    try:
        err = stderr_append.open("ab") if stderr_append is not None else out
        try:
            proc = subprocess.Popen(  # noqa: S603 — the command is composed from repo-internal literals
                launch.command,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                creationflags=creationflags,
                close_fds=True,
            )
        finally:
            if stderr_append is not None:
                err.close()
    finally:
        out.close()

    try:
        create_time = psutil.Process(proc.pid).create_time()
    except psutil.NoSuchProcess:
        # The process already exited (instant failure). Record a sentinel that
        # can never match a real process start time: a pid freed by an
        # instantly-dead child is at its most reusable, and recording
        # time.time() would make `_process_for_record`'s create-time check
        # accept the pid's next innocent occupant — a later kill_session
        # could then kill an unrelated process tree (audit 2026-08-08 P2).
        create_time = _DEAD_CHILD_SENTINEL
    SessionRecord(
        pid=proc.pid,
        create_time=create_time,
        cmd=launch.display,
        cwd=str(cwd),
        started_at=time.time(),
    ).write(_record_path(name))
    return True


def graceful_signal(name: str) -> bool:
    """Send the graceful-stop signal (Ctrl-Break) to the session's process group
    without waiting or force-killing.

    The Windows analog of posixproc.graceful_signal: the agent maps Ctrl-Break to
    its shutdown path (runs finally). Returns True if a live process was signaled.
    `ava stop`'s reap uses this to signal every agent, then wait on all of them
    under one shared deadline before force-killing stragglers.
    """
    import signal

    rec = _read_record(name)
    if rec is None:
        return False
    proc = _process_for_record(rec)
    if proc is None:
        return False
    with contextlib.suppress(*_GONE):
        proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]  # Windows-only signal; called only on Windows
        return True
    return False


def _live_session_pids(exclude: str) -> set[int]:
    """Recorded pids of every live session on this host except `exclude`."""
    pids: set[int] = set()
    for rec_file in _sessions_dir().glob("*.json"):
        if rec_file.stem == exclude:
            continue
        rec = SessionRecord.read(rec_file)
        if rec is not None and _process_for_record(rec) is not None:
            pids.add(rec.pid)
    return pids


def _self_ancestry_below(target_pid: int) -> set[int]:
    """This process plus its ancestors, walking up and stopping at `target_pid`.

    Only the part of the chain *below* the process being killed is returned: the
    target itself is the thing the caller asked to stop, and nothing above it can
    be inside its subtree (if an ancestor of ours were a descendant of the target,
    the target would be one of our ancestors and the walk would have stopped
    there).
    """
    pids = {os.getpid()}
    with contextlib.suppress(*_GONE):
        for ancestor in psutil.Process().parents():
            if ancestor.pid == target_pid:
                break
            pids.add(ancestor.pid)
    return pids


def _spared_pids(name: str, proc: psutil.Process) -> frozenset[int]:
    """Pids whose subtrees a kill of session `name` must leave alone.

    Windows keeps the spawner in the parent chain forever (module docstring), so
    without this every session a daemon started is reachable from a tree walk
    rooted at that daemon — and the hard kill goes children-first, so the
    *spawned* session dies before its spawner does. Two rules restore the same
    invariant that a session's lifetime belongs only to the caller that names it:

    - **every other live session.** This is the bug of 2026-07-29: a
      gateway-triggered self-update spawns the `ava-updater` session from inside
      the ops daemon, and the updater's own `ava restart` stops this host's
      services — including `ava-ops`. Killing `ava-ops`'s tree killed the updater
      (its child) first, so the Windows agent-runner ended up stopped *and*
      un-updated, with the log ending mid-teardown right after the watchdog line.
      Agent processes are records in the same namespace and were exposed the same
      way: `ava cluster update`'s stop deliberately leaves them running for the rollout to
      quiesce, yet stopping `ava-ops` force-killed every agent it had launched,
      skipping the graceful path their `finally` needs.
    - **this process and its own ancestors below `proc`.** Whoever is running the
      kill has to survive it. This holds even if the caller's own session record
      was already reaped, so the fix does not depend on on-disk state.
    """
    return frozenset(_live_session_pids(name) | _self_ancestry_below(proc.pid))


def tree_kill_would_spare(session_name: str, proc: psutil.Process, ancestor_pids: set[int]) -> bool:
    """Whether killing `session_name`'s tree leaves this lineage untouched.

    POSIX launch reparenting already isolates sessions, so a recorded session in
    a lineage is always reached by its stop tree. Windows needs the opposite
    answer: `_spared_pids` preserves nested live sessions and the kill caller's
    ancestry, the 2026-07-29 boundary that keeps an updater alive while it stops
    its `ava-ops` parent. The 2026-08-24 restart guard asks this same predicate
    rather than trying to duplicate the kill path's boundary.
    """
    if not IS_WINDOWS:
        return False
    return bool(_spared_pids(session_name, proc) & ancestor_pids)


def _descendants(proc: psutil.Process, *, spare: frozenset[int]) -> list[psutil.Process]:
    """`proc`'s descendants, pruning every subtree rooted at a spared pid.

    Pruning the whole subtree (not just the spared process) is the point: the
    spared session's own children — the `ava restart` under the updater's cmd.exe
    — are as much a part of that session as its top process.
    """
    out: list[psutil.Process] = []
    stack: list[psutil.Process] = []
    with contextlib.suppress(*_GONE):
        stack = proc.children()
    while stack:
        child = stack.pop()
        if child.pid in spare:
            continue
        out.append(child)
        with contextlib.suppress(*_GONE):
            stack.extend(child.children())
    return out


def _terminate_tree(
    proc: psutil.Process, *, graceful: bool, timeout: float, spare: frozenset[int] = frozenset()
) -> None:
    """Terminate `proc` and its descendants (cmd -> uv -> python, …), except the
    subtrees rooted at a pid in `spare` (see `_spared_pids`).

    graceful: first deliver Ctrl-Break to the process group and wait up to
    `timeout` for a clean exit (the daemons map Ctrl-Break/SIGBREAK to their
    shutdown path); then hard-terminate whatever remains. force: terminate the
    tree immediately.
    """
    children = _descendants(proc, spare=spare)
    if graceful:
        import signal

        with contextlib.suppress(*_GONE):
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]  # Windows-only signal; this branch only runs on Windows
        _gone, alive = psutil.wait_procs([proc, *children], timeout=timeout)
        if not alive:
            return
    # Hard kill: the session's own process last, so it cannot respawn one of the
    # children already taken down.
    for p in [*children, proc]:
        with contextlib.suppress(*_GONE):
            p.kill()
    psutil.wait_procs([proc, *children], timeout=5)


def kill_session(name: str, *, graceful: bool = False, timeout: float = 15.0) -> tuple[bool, str]:
    """Stop the named session and forget it (force / graceful C-c).

    Stops only *this* session: the tree walk prunes the other live sessions and
    the caller's own ancestry (`_spared_pids`), because on Windows a session
    spawned by a daemon never stops being that daemon's descendant.

    Returns (ok, mode) with mode in {graceful, forced, noop} to match
    `_graceful_kill_session`. Idempotent: an absent/dead session is a noop.

    `ok` is the SessionBackend contract's "confirmed gone", so the record is
    dropped only once the session's process actually stopped. Unlinking it
    regardless would answer `has_session` False for a process that is still
    running and still holding its port, and the caller's response to False is to
    launch the service again — `[Errno 48] Address already in use`, in a stop that
    reported success. Leaving the record instead keeps the survivor visible to
    every consumer that asks (issue #1015).
    """
    rec = _read_record(name)
    if rec is None:
        return True, "noop"
    proc = _process_for_record(rec)
    if proc is None:
        _record_path(name).unlink(missing_ok=True)
        return True, "noop"
    try:
        _terminate_tree(proc, graceful=graceful, timeout=timeout, spare=_spared_pids(name, proc))
    except Exception as exc:
        logger.warning("winproc kill {name} hit {exc}", name=name, exc=exc)
    mode = "graceful" if graceful else "forced"
    if proc.is_running():
        logger.error(
            "winproc kill {name}: pid {pid} is still running after the kill — leaving its "
            "session record in place so the survivor stays visible",
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
