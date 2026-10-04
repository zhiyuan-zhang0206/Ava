"""One live pty session inside the service: shell child, master fd, screen, transcript.

The service owns every master. A session is the `bash -l -i` child of the
service on its own pty. The service's event loop reads the master and feeds a
lazily built pyte screen model, a bounded raw ring and the byte transcript
(`PtySession.feed`); this module holds what a session is and the blocking steps
around it (fork, kill, teardown).
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import os
import pty
import signal
import struct
import termios
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from base.log import logger
from base.native_process.ownership import OwnedProcess, stable_create_time
from base.sessions import log_prefix
from base.sessions.pty import session_tree
from base.sessions.record import SessionRecord

# A pid is "the same process we launched" only if its start-time matches to
# within this tolerance when no start tick is recorded (mirrors posixproc).
_CREATE_TIME_TOLERANCE_S = 2.0

# Graceful kill: SIGTERM to every session member, wait this long for the
# reader to observe the exit before the SIGKILL sweep.
_KILL_WAIT_S = 5.0

# After SIGKILL, how long to wait for the members to exit and for the
# reader's cleanup before concluding the kill failed.
_KILL_FORCE_WAIT_S = 3.0

# After a signal, how long to poll waitpid for the child to die into a
# reapable state before giving up (SIGKILL delivery lags under load).
_CHILD_EXIT_POLL_S = 2.0

# How many bytes one master read may return (pty buffers are ~4-64 KB). While a
# live pyte screen is fed on the event loop, a read is small so one chatty session
# holds the loop for milliseconds, not a tenth of a second (pyte parses ~0.5 MB/s).
READ_CHUNK = 65536
READ_CHUNK_LIVE_SCREEN = 4096

# Per-session byte transcript is a best-effort debug aid; cap it so a long-lived
# shell cannot grow without bound. Past the cap the service stops appending.
_TRANSCRIPT_CAP_BYTES = 64 * 1024 * 1024

# Raw ring buffer cap for lazy screen replay. Full-screen redraws and scrolling
# self-heal after truncation, so the bounded tail matches finite scrollback.
_RAW_RING_CAP = 2 * 1024 * 1024

# Prompt-wait cap before the initial command is written anyway (a busy CI box
# can init an interactive shell slowly); then a settle beat.
_INITIAL_CMD_READY_TIMEOUT_S = 30.0
_INITIAL_CMD_SETTLE_S = 0.3

# The shell: a login interactive bash, the classic pane shape.
SHELL_ARGV = ("/bin/bash", "-l", "-i")
SHELL_COMMAND = " ".join(SHELL_ARGV)

# Sentinel for a child whose create_time could not be read (died at spawn: the
# pid is at its most reusable moment); it can never match a reused pid.
DEAD_CHILD_SENTINEL = -1.0


def set_winsz(fd: int, cols: int, rows: int) -> None:
    """TIOCSWINSZ on `fd` (master or the child's slave)."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def fork_shell(cwd: str, env: dict[str, str], cols: int, rows: int) -> tuple[int, int]:
    """pty.fork the login shell; returns (pid, master_fd). The child never returns.

    Called from a request thread of a multi-threaded process, so the child does
    only what cannot wait on a lock another thread held at the fork: a window
    size ioctl, chdir, signal dispositions and exec with an environment built
    beforehand. The master fd is made non-inheritable at once; the allocation
    lock serializes forks, so no sibling shell is forked while it is inheritable.
    """
    pid, master = pty.fork()
    if pid == 0:  # child: the login shell
        try:
            set_winsz(0, cols, rows)  # fd 0 = the pty slave
            os.chdir(cwd)
            # Ignored dispositions survive exec: reset them or the shell's jobs
            # never receive a closure's per-job TERM (#2045).
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGPIPE):
                signal.signal(sig, signal.SIG_DFL)
            os.execve(SHELL_ARGV[0], list(SHELL_ARGV), env)  # noqa: S606 — the pty child execs the login shell directly; a wrapper would defeat the pty
        except BaseException:
            os._exit(127)
    os.set_inheritable(master, False)  # noqa: FBT003 — positional-only
    return pid, master


class PtySession:
    """The one live pty session: child, master fd, screen model and transcript.

    The pyte screen model is built lazily on the first screen need (capture /
    prompt wait): until then output accumulates in a bounded raw ring the
    screen is replayed from, keeping capture-free sessions pyte-free.
    """

    def __init__(
        self,
        name: str,
        pid: int,
        master_fd: int,
        cols: int,
        rows: int,
        record: SessionRecord,
        log_path: Path,
        *,
        log_cap: int = _TRANSCRIPT_CAP_BYTES,
    ) -> None:
        self.name = name
        self.pid = pid
        self.master_fd = master_fd
        self.cols = cols
        self.rows = rows
        self.record = record
        # Any keeps the screen module and pyte lazy; naming PtyScreen here
        # would import them eagerly (no TYPE_CHECKING by repo convention).
        self._screen: Any = None
        # The session's membership as the ledger snapshots last saw it.
        self.capture = session_tree.SessionCapture(self.shell, [self.shell], None)
        self._ring = bytearray()
        self._log_fd, created = log_prefix.open_session_log(log_path, name, pid=pid)
        self._log_written = os.fstat(self._log_fd).st_size if created else 0
        self._log_cap = log_cap
        # Driven by the service's loop: the initial command waiting on the prompt,
        # and whether the teardown has been requested.
        self.initial: InitialCommand | None = None
        self.ending = False
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._dead = False
        self._cond = threading.Condition(self._lock)

    @property
    def shell(self) -> OwnedProcess:
        """The shell's recorded birth identity."""
        return OwnedProcess(self.pid, self.record.create_time, self.record.starttime)

    def feed(self, data: bytes) -> None:
        """Ingest output: the live screen when one exists, the raw ring otherwise.

        Called on the service's event loop, so it never waits on the screen
        build (`screen`): bytes that arrive meanwhile land in the fresh ring and
        are replayed before the screen is published. pyte must never take the
        session down (a fidelity bug would kill the shell with it): feed errors
        are swallowed and the ring remains the degraded capture source.
        """
        with self._lock:
            screen = self._screen
            if screen is None:
                self._ring += data
                if len(self._ring) > _RAW_RING_CAP:
                    del self._ring[: len(self._ring) - _RAW_RING_CAP]
                return
        with contextlib.suppress(Exception):
            screen.feed(data)

    def read_size(self) -> int:
        """How many bytes the next master read may take."""
        return READ_CHUNK if self.live_screen() is None else READ_CHUNK_LIVE_SCREEN

    def screen(self) -> Any:
        """The pyte model (a ``screen.PtyScreen``), built on first need by replaying the ring.

        Replaying a full ring takes seconds, so it runs outside the session lock:
        output that arrives during the replay accumulates in a fresh ring and is
        folded in until the ring is empty, and the screen is published in the
        same critical section as that last (empty) check, so no byte is lost.
        """
        with self._build_lock:
            with self._lock:
                if self._screen is not None:
                    return self._screen
                replay, self._ring = bytes(self._ring), bytearray()
            from base.sessions.pty.screen import PtyScreen

            screen = PtyScreen(self.cols, self.rows)
            late = replay
            while True:
                with contextlib.suppress(Exception):
                    screen.feed(late)
                with self._lock:
                    late, self._ring = bytes(self._ring), bytearray()
                    if not late:
                        self._screen = screen
                        return screen

    def live_screen(self) -> Any:
        """The screen model when one was built, else None (never builds one)."""
        with self._lock:
            return self._screen

    def log_write(self, data: bytes) -> None:
        """Append to the byte transcript, up to the per-session cap.

        Best-effort: past the cap (or on a write error) the service stops
        appending but the session keeps running. The event loop only.
        """
        if self._log_written >= self._log_cap:
            return
        room = self._log_cap - self._log_written
        try:
            written = os.write(self._log_fd, data[:room])
        except OSError:
            return  # disk full / fd gone: stop trying, keep the session
        self._log_written += written

    @property
    def dead(self) -> bool:
        with self._lock:
            return self._dead

    def begin_finish(self) -> bool:
        """Atomically claim the teardown; exactly one caller wins.

        The check-and-set must share one lock: the reader seeing EOF while a
        request thread's write observes the death would otherwise both close
        the master fd, and a double close can kill an unrelated fd this
        process has since opened.
        """
        with self._cond:
            if self._dead:
                return False
            self._dead = True
            self._cond.notify_all()
            return True

    def wait_dead(self, timeout: float) -> bool:
        with self._cond:
            self._cond.wait_for(lambda: self._dead, timeout)
            return self._dead

    def pid_matches(self) -> bool:
        """True when this shell's pid has not been recycled and the shell still runs."""
        try:
            proc = psutil.Process(self.pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return False
            if self.record.starttime is not None:
                return self.record.identifies(self.pid) is True
            return (
                abs(stable_create_time(proc) - self.record.create_time) <= _CREATE_TIME_TOLERANCE_S
            )
        except psutil.Error:
            return False

    def write(self, data: bytes) -> None:
        """Write `data` to the master (blocking); OSError when the session is gone."""
        view = memoryview(data)
        while view:
            written = os.write(self.master_fd, view)
            if written == 0:
                break  # a blocking pty master should never report 0; bail instead of spinning
            view = view[written:]

    def resize(self, cols: int, rows: int) -> None:
        set_winsz(self.master_fd, cols, rows)
        self.cols, self.rows = cols, rows
        live = self.live_screen()
        if live is not None:
            live.resize(rows, cols)
        # The foreground TUI redraws on SIGWINCH, delivered to the pane's
        # process group, so signal the group, not just the shell.
        if self.pid_matches():
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(self.pid, signal.SIGWINCH)


def reap_child(session: PtySession) -> None:
    """Reap the session's child, killing it if it outlived its session.

    The reader's EOF path can beat its own reap check (EOF arrives the same pass
    a kill lands), leaving the child a zombie, which answers ``pid_exists``
    True. And a child still alive when its session ends (slave fully closed
    under it) must not survive as an orphan: hang it up, then kill it.
    """
    try:
        pid, _status = os.waitpid(session.pid, os.WNOHANG)
    except ChildProcessError:
        return  # already reaped (or the pid was recycled onto a non-child)
    if pid:
        return
    for sig in (signal.SIGHUP, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, OSError):
            os.kill(session.pid, sig)
        # SIGKILL delivery can lag on a loaded box; a single WNOHANG reap right
        # after can return 0 and leave an UNREAPED zombie. Poll briefly.
        deadline = time.monotonic() + _CHILD_EXIT_POLL_S
        while time.monotonic() < deadline:
            try:
                pid, _status = os.waitpid(session.pid, os.WNOHANG)
            except ChildProcessError:
                return
            if pid:
                return
            time.sleep(0.02)


def finish(session: PtySession, on_end: Callable[[PtySession], None]) -> None:
    """End-of-life teardown. Idempotent: one caller runs it.

    ORDER MATTERS: `on_end` first, the moment the teardown is claimed. It drops
    the session from the service table, so from that instant a concurrent
    same-name `new` sees no session and can never adopt this dying one as its
    success. Only then the slow parts run: reap the child (bounded polls), close
    the master (hangs up the slave's foreground group) and the transcript.
    """
    if not session.begin_finish():
        return
    on_end(session)
    reap_child(session)
    with contextlib.suppress(OSError):
        os.close(session.master_fd)
    with contextlib.suppress(OSError):
        os.close(session._log_fd)
    logger.info("pty session ended: {name} (pid={pid})", name=session.name, pid=session.pid)


class InitialCommand:
    """Submits the session's initial command once its login shell is READY.

    A pre-ready write is flushed by the shell's own tcsetattr(TCSAFLUSH) during
    interactive init (the loss observed under a slow CI login shell). Readiness is
    the prompt: bash prints it only after setting its terminal modes. The service
    ticks `step` until it is `done`, so a slow login shell blocks no request.
    (Watching the prompt builds the pyte screen: an initial-command session pays
    the import; a plain interactive shell does not.)
    """

    TICK_S = 0.1

    def __init__(self, session: PtySession, cmd: str) -> None:
        self._session = session
        self._cmd = cmd
        self._give_up_at = time.monotonic() + _INITIAL_CMD_READY_TIMEOUT_S
        self._send_at: float | None = None
        self.done = False

    def step(self) -> None:
        """Note the prompt, then write the command once the shell has settled."""
        now = time.monotonic()
        if self._send_at is None:
            prompt = self._session.screen().current_line()
            if prompt.endswith(("$", "#")) or now >= self._give_up_at:
                self._send_at = now + _INITIAL_CMD_SETTLE_S
            return
        if now >= self._send_at:
            self.done = True
            # A just-dead session must not crash the service; the write is best-effort.
            # A runner that never started is covered by schedule reconcile/breaker.
            with contextlib.suppress(OSError):
                os.write(self._session.master_fd, self._cmd.encode() + b"\r")


def kill_session(session: PtySession, *, graceful: bool) -> dict[str, Any]:
    """End the session: every process in the shell's tree and POSIX session.

    Job control gives each job its own process group, so a group signal never
    reaches `cmd &`; the membership rule and its boundary (a setsid'd process
    that left the tree is sovereign) live in `session_tree`. Returns the verdict
    ``{"mode", "interrupted"[, "survivors"]}``; RuntimeError names a failed kill.
    """
    if not session.pid_matches():
        # The shell died but the reader has not finished yet; its own reap
        # check runs `finish` within one poll: report the noop.
        logger.warning(
            "pty kill {name}: recorded pid {pid} no longer matches",
            name=session.name,
            pid=session.pid,
        )
        return {"mode": "noop", "interrupted": False}
    shell = session.shell
    # The interrupted verdict is snapshotted HERE, in the same request that
    # kills; a separate idle probe cannot close that TOCTOU. Any live member
    # beyond the shell is running work; a shell that no longer verifies answers
    # busy (fail-open: it cannot be proven idle).
    members = session_tree.session_members(shell)
    interrupted = len(members) != 1
    mode = "forced"
    if graceful:
        session_tree.terminate(members)
        if session.wait_dead(_KILL_WAIT_S):
            mode = "graceful"
    # Runs even after a graceful death: a TERM-ignoring job outlives its shell.
    result = session_tree.kill_session_tree(shell, also=members, wait_s=_KILL_FORCE_WAIT_S)
    if result.killed:
        mode = "forced"
    if result.stuck:
        pids = sorted(identity.pid for identity in result.stuck)
        raise RuntimeError(f"session {session.name}: processes survived the kill: {pids}")
    if not session.wait_dead(_KILL_FORCE_WAIT_S):
        raise RuntimeError(f"session {session.name} survived the kill")
    if not result.survivors:
        return {"mode": mode, "interrupted": interrupted}
    # Survivors left at this point are only processes this user may not signal (a
    # root `sudo` on the pty): the session is over, and it cut short the work
    # they did: `interrupted`, with them named.
    return {
        "mode": mode,
        "interrupted": True,
        "survivors": sorted(identity.pid for identity in result.survivors),
    }


def decode_data(value: object) -> bytes:
    """The bytes of a base64 `data` field (ValueError or TypeError when it is not base64 text)."""
    if not isinstance(value, str):
        raise TypeError("data must be base64 text")
    return base64.b64decode(value, validate=True)
