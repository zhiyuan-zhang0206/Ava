"""One detached PTY host per persistent agent shell.

Usage: ``python -m shared.pty_sessions.host <name> <cwd> <envfile> <record>
<socket> <transcript> <generation> [cmd_b64]``.

The spawner reparents this process through ``shared._reparent``. No service
roster or infra teardown can reach it by process tree; only its ``kill`` op,
the shell exiting, or this host crashing ends the session. That sovereignty is
the SDK's persistence guarantee (decisions/2026-08-13-per-session-pty-hosts.md).

One host owns the login-shell child, PTY master, bounded raw ring, optional
lazy pyte screen, byte transcript, identity record, and JSON-line Unix socket.
The 0600 env file and all paths arrive resolved on argv. Per-session memory is
fleet memory, so this module avoids the Settings import chain and loads pyte
only when capture or initial-command prompt detection needs it.

On child death the reader immediately unlinks the record and socket before
closing the master, preventing a dying host from being adopted by a concurrent
same-name spawn. SIGHUP/SIGTERM/SIGPIPE are ignored; ending a session remains
the ``kill`` op's responsibility. SIGKILL has one-session blast radius.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import json
import os
import pty
import re
import select
import shlex
import signal
import socket
import struct
import sys
import termios
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import psutil

from shared.log import logger
from shared.pty_sessions._paths import DEFAULT_COLS, DEFAULT_ROWS, err, ok, write_record
from shared.session_record import SessionRecord, pid_starttime_ticks

# A pid is "the same process we launched" only if its start-time matches to
# within this tolerance — guards against the OS recycling the pid onto an
# unrelated process after ours exits (mirrors posixproc).
_CREATE_TIME_TOLERANCE_S = 2.0

# Sentinel for a child whose create_time could not be read (died at spawn —
# the pid is at its most reusable moment): can never match a reused pid.
_DEAD_CHILD_SENTINEL = -1.0

# Graceful kill: SIGTERM to the shell's group, wait this long for the reader
# to observe the exit before escalating to SIGKILL.
_KILL_WAIT_S = 5.0

# After SIGKILL, how long to wait for the reader's cleanup before the tree
# backstop (psutil walk) and before concluding the kill failed.
_KILL_FORCE_WAIT_S = 3.0

# After a signal, how long to poll waitpid for the child to die into a
# reapable state before giving up (SIGKILL delivery lags under load).
_REAP_POLL_S = 2.0

# Reader loop select timeout — bounds how long kill/exit detection can lag.
_READER_POLL_S = 1.0

# How many bytes a single master read may return (pty buffers are ~4-64 KB).
_READ_CHUNK = 65536

# Per-session byte transcript is a best-effort debug aid; cap it so a long-lived
# shell cannot grow without bound. Past the cap the host stops appending.
_TRANSCRIPT_CAP_BYTES = 64 * 1024 * 1024

# Raw ring buffer cap for lazy screen replay. Full-screen redraws and scrolling
# self-heal after truncation, so the bounded tail matches finite scrollback.
_RAW_RING_CAP = 2 * 1024 * 1024

# Session names ride argv and the record filename; keep the same conservative
# slug shape ava/shell/sessions.py enforces for its names.
_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")

# A request line this long is garbage, not a session op.
_MAX_REQUEST_BYTES = 1 << 20

# After the session dies, how long the socket keeps draining so an in-flight
# kill's response reaches its caller before the process exits.
_EXIT_DRAIN_S = 0.5


def _set_winsz(fd: int, cols: int, rows: int) -> None:
    """TIOCSWINSZ on `fd` (master or the child's slave)."""
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _load_env_file(path: str) -> dict[str, str]:
    """Parse a 0600 session-env file (KEY=quoted-value lines) into a dict.

    The writer lives in cli.py (``write_env_file``) and mirrors
    ``shared.session_env.env_load_prefix``'s on-disk format. Parsed with
    shlex so quoted values — spaces, newlines — survive; a comment or a
    non-assignment token is skipped.
    """
    env: dict[str, str] = {}
    content = Path(path).read_text(encoding="utf-8")
    for token in shlex.split(content, comments=True):
        key, sep, value = token.partition("=")
        if not sep or not key:
            continue
        env[key] = value
    return env


class PtySession:
    """The one live pty session this host carries: child, master fd, screen.

    The pyte screen model is built lazily on the first screen need (capture /
    prompt wait): until then output accumulates in a bounded raw ring the
    screen is replayed from, keeping capture-free hosts pyte-free.
    """

    def __init__(
        self,
        name: str,
        pid: int,
        master_fd: int,
        cols: int,
        rows: int,
        record: SessionRecord,
        rec_path: Path,
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
        self.record_path = rec_path
        # The pyte screen, typed as Any: naming PtyScreen here would import
        # the screen module (and pyte) at module load — the laziness this
        # class exists to provide (no TYPE_CHECKING by repo convention).
        self._screen: Any = None
        self._ring = bytearray()
        self._log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        self._log_written = 0
        self._log_cap = log_cap
        self._lock = threading.Lock()
        self._dead = False
        self._cond = threading.Condition(self._lock)

    def feed(self, data: bytes) -> None:
        """Reader-thread ingest: the live screen when one exists, the raw
        ring otherwise. pyte must never take the session down (a fidelity
        bug would kill the shell with it) — feed errors are swallowed and
        the ring remains the degraded capture source."""
        with self._lock:
            screen = self._screen
            if screen is None:
                self._ring += data
                if len(self._ring) > _RAW_RING_CAP:
                    del self._ring[: len(self._ring) - _RAW_RING_CAP]
                return
        with contextlib.suppress(Exception):
            screen.feed(data)

    def screen(self) -> Any:
        """The pyte model (a ``screen.PtyScreen``), built on first need by
        replaying the ring."""
        with self._lock:
            if self._screen is None:
                from shared.pty_sessions.screen import PtyScreen

                screen = PtyScreen(self.cols, self.rows)
                with contextlib.suppress(Exception):
                    screen.feed(bytes(self._ring))
                self._ring = bytearray()
                self._screen = screen
            return self._screen

    def log_write(self, data: bytes) -> None:
        """Append to the byte transcript, up to the per-session cap.

        Best-effort: past the cap (or on a write error) the host stops
        appending but the session keeps running — the transcript is a debug
        aid, never a session-critical sink. Called from the reader thread only.
        """
        if self._log_written >= self._log_cap:
            return
        room = self._log_cap - self._log_written
        try:
            written = os.write(self._log_fd, data[:room])
        except OSError:
            return  # disk full / fd gone — stop trying, keep the session
        self._log_written += written

    @property
    def dead(self) -> bool:
        with self._lock:
            return self._dead

    def begin_finish(self) -> bool:
        """Atomically claim the teardown; exactly one caller wins.

        The check-and-set must share one lock: the reader seeing EOF while a
        request thread's write/kill observes the death would otherwise both
        pass a dead-check and both close the master fd — a double close can
        kill an unrelated fd this process has since opened.
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
        """True when this shell's pid has not been recycled."""
        try:
            proc = psutil.Process(self.pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return False
            if self.record.starttime is not None:
                return self.record.identifies(self.pid) is True
            return abs(proc.create_time() - self.record.create_time) <= _CREATE_TIME_TOLERANCE_S
        except psutil.Error:
            return False


def _reader_loop(session: PtySession, sock_file: Path) -> None:
    """Read the master fd until the shell dies; then tear the session down.

    The loop reaps the child (waitpid WNOHANG) on every pass in addition to
    watching for EOF: the shell can exit while a background child still holds
    the slave open (no EOF), and the session dies with the shell — the pane
    semantics.
    """
    while True:
        try:
            ready, _, _ = select.select([session.master_fd], [], [], _READER_POLL_S)
        except (OSError, ValueError) as exc:
            logger.warning("pty reader {name}: select failed: {exc}", name=session.name, exc=exc)
            break
        if ready:
            try:
                data = os.read(session.master_fd, _READ_CHUNK)
            except OSError as exc:
                logger.warning("pty reader {name}: read failed: {exc}", name=session.name, exc=exc)
                break
            if not data:
                break  # EOF on master
            session.feed(data)
            session.log_write(data)
        try:
            reaped, _status = os.waitpid(session.pid, os.WNOHANG)
        except ChildProcessError as exc:
            logger.warning(
                "pty reader {name}: waitpid {pid} failed: {exc}",
                name=session.name,
                pid=session.pid,
                exc=exc,
            )
            break
        if reaped:
            break
    _finish(session, sock_file)


def _reap_child(session: PtySession) -> None:
    """Reap the session's child, killing it if it outlived its session.

    The reader's EOF path can beat its own reap check (EOF arrives the same
    loop pass a kill lands), leaving the child a zombie — a zombie answers
    ``pid_exists`` True, so liveness checks would lie about the session.
    Also: a child still alive when its session ends (slave fully closed under
    it) must not survive as an orphan — hang it up, then kill it.
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
        # SIGKILL delivery can lag on a loaded box; a single WNOHANG reap
        # right after can return 0 and leave the child an UNREAPED zombie —
        # a zombie answers psutil.pid_exists True, so `kill` would report
        # success while liveness checks still see the pid. Poll briefly.
        deadline = time.monotonic() + _REAP_POLL_S
        while time.monotonic() < deadline:
            try:
                pid, _status = os.waitpid(session.pid, os.WNOHANG)
            except ChildProcessError:
                return  # already reaped (or the pid was recycled)
            if pid:
                return
            time.sleep(0.02)


def _finish(session: PtySession, sock_file: Path) -> None:
    """End-of-life teardown, then process exit.

    ORDER MATTERS (P2 ghost-success review): the record and socket are
    unlinked FIRST, the moment the teardown is claimed — from that instant a
    concurrent same-name ``new`` sees no session (`has` false) and no
    answering socket, so it can never adopt this dying host as its success.
    Only then the slow parts run: reap the child (bounded polls), close the
    master (hangs up the slave's foreground group), close the log, and —
    after a short drain so an in-flight kill's response reaches its caller —
    exit the host. Idempotent against a concurrent kill; one caller runs it.
    """
    if not session.begin_finish():
        return  # another thread won the teardown
    with contextlib.suppress(OSError):
        session.record_path.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        sock_file.unlink(missing_ok=True)
    _reap_child(session)
    with contextlib.suppress(OSError):
        os.close(session.master_fd)
    with contextlib.suppress(OSError):
        os.close(session._log_fd)
    logger.info("pty session ended: {name} (pid={pid})", name=session.name, pid=session.pid)

    def _exit_soon() -> None:
        time.sleep(_EXIT_DRAIN_S)
        os._exit(0)

    threading.Thread(target=_exit_soon, daemon=True).start()


# ---------------------------------------------------------------------------
# Request handlers — each returns a response dict for the CLI.
# ---------------------------------------------------------------------------


def _op_ping(session: PtySession, req: dict[str, Any]) -> dict[str, Any]:
    """Readiness + identity. A DYING session answers err 3 (P2 review): the
    spawner's ready wait and the double-start guard must never read a host
    that has claimed its teardown as a live owner of the name."""
    del req
    if session.dead:
        return err(3, f"no such pty session: {session.name}")
    return ok({"pid": session.pid, "host_pid": os.getpid()})


def _op_send(session: PtySession, req: dict[str, Any]) -> dict[str, Any]:
    if session.dead:
        return err(3, f"no such pty session: {session.name}")
    try:
        data = base64.b64decode(req.get("data") or "", validate=True)
    except ValueError:
        return err(2, "send requires base64 text as its single argument")
    view = memoryview(data)
    while view:
        try:
            written = os.write(session.master_fd, view)
        except OSError as exc:
            return err(1, f"session {session.name} is gone: {exc}")
        if written == 0:
            break  # a blocking pty master should never report 0; bail instead of spinning
        view = view[written:]
    return ok()


def _op_capture(session: PtySession, req: dict[str, Any]) -> dict[str, Any]:
    if session.dead:
        return err(3, f"no such pty session: {session.name}")
    try:
        lines = int(req.get("lines", 200))
    except (TypeError, ValueError):
        lines = 200
    lines = max(1, min(lines, 100000))  # CLI caps at 100000; clamp direct dialers
    text = session.screen().render(lines, scrollback=bool(req.get("scrollback", True)))
    return ok({"text": text})


def _op_resize(session: PtySession, req: dict[str, Any]) -> dict[str, Any]:
    if session.dead:
        return err(3, f"no such pty session: {session.name}")
    try:
        cols, rows = int(req.get("cols") or 0), int(req.get("rows") or 0)
    except ValueError:
        return err(2, "resize requires integer cols and rows")
    if cols < 1 or rows < 1 or cols > 10000 or rows > 10000:
        return err(2, f"resize out of range: {cols}x{rows}")
    _set_winsz(session.master_fd, cols, rows)
    session.cols, session.rows = cols, rows
    with session._lock:
        live_screen = session._screen
    if live_screen is not None:
        live_screen.resize(rows, cols)
    # The foreground TUI redraws on SIGWINCH, delivered to the pane's
    # process group, so signal the group, not just the shell.
    if session.pid_matches():
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(session.pid, signal.SIGWINCH)
    return ok()


def _kill_target_groups(session: PtySession) -> set[int]:
    """The process groups a kill must signal: the shell's own group and the
    tty's current foreground group (the foreground job lives in its own pgrp).

    The hangup-on-master-close only reaches the foreground job on some
    platforms (macOS yes; Linux observed not), so the kill must signal it
    explicitly — `os.tcgetpgrp` on the master answers who owns the tty.
    """
    groups = {session.pid}
    try:
        foreground = os.tcgetpgrp(session.master_fd)
    except OSError:
        foreground = -1  # no foreground group (tty already gone)
    if foreground > 0:
        groups.add(foreground)
    return groups


def _session_busy(session: PtySession) -> bool:
    """Whether the session carries live work beyond its idle shell.

    Idle = the shell sits at its prompt: no foreground job owns the tty (the
    same signal `_kill_target_groups` uses) and no descendant survives.
    A shell that cannot be inspected answers busy (fail-open: a session we
    cannot prove idle may well be running work).
    """
    if session.dead or not session.pid_matches():
        return False
    try:
        foreground = os.tcgetpgrp(session.master_fd)
    except OSError:
        foreground = -1
    if foreground > 0 and foreground != session.pid:
        return True
    try:
        return bool(psutil.Process(session.pid).children(recursive=True))
    except psutil.Error:
        return True  # cannot inspect — assume the worst


def _op_kill(session: PtySession, req: dict[str, Any]) -> dict[str, Any]:
    if session.dead:
        return ok({"mode": "noop", "interrupted": False})  # idempotent, like posixproc
    graceful = bool(req.get("graceful", False))
    # The interrupted verdict is snapshotted HERE, in the same request that
    # kills — a job starting after a separate idle probe could otherwise be
    # cut short with no notice (the TOCTOU a standalone probe cannot close).
    interrupted = _session_busy(session)

    if not session.pid_matches():
        # The shell died but the reader has not finished yet; the reader's
        # own reap check will run _finish within one poll — report the noop.
        logger.warning(
            "pty kill {name}: recorded pid {pid} no longer matches",
            name=session.name,
            pid=session.pid,
        )
        return ok({"mode": "noop"})

    mode = "forced"
    groups = _kill_target_groups(session)
    if graceful:
        for grp in groups:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(grp, signal.SIGTERM)
        if session.wait_dead(_KILL_WAIT_S):
            mode = "graceful"
    if not session.dead:
        # Escalate: SIGKILL the shell's group and the foreground job's group
        # (independent pgrps), then any surviving descendants via a psutil
        # walk — the tree must not leave orphans behind.
        for grp in groups:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(grp, signal.SIGKILL)
        if not session.wait_dead(_KILL_FORCE_WAIT_S):
            with contextlib.suppress(psutil.Error):
                proc = psutil.Process(session.pid)
                for child in [*proc.children(recursive=True), proc]:
                    with contextlib.suppress(psutil.Error):
                        child.kill()
            session.wait_dead(_KILL_FORCE_WAIT_S)
    if session.dead:
        return ok({"mode": mode, "interrupted": interrupted})
    # The kill did not take — keep the record so the caller can see the
    # survivor (posixproc's #1015 lesson), and report failure.
    return err(1, f"session {session.name} survived the kill")


_OPS: dict[str, Callable[[PtySession, dict[str, Any]], dict[str, Any]]] = {
    "ping": _op_ping,
    "send": _op_send,
    "send_keys": _op_send,  # keys arrive pre-translated to bytes (cli side)
    "capture": _op_capture,
    "resize": _op_resize,
    "kill": _op_kill,
}


def _read_line(conn: socket.socket) -> bytes | None:
    buf = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            return None
        buf += chunk
        if b"\n" in buf:
            return buf.split(b"\n", 1)[0]
        if len(buf) > _MAX_REQUEST_BYTES:
            raise ValueError(f"request exceeds {_MAX_REQUEST_BYTES} bytes")


def _respond(conn: socket.socket, resp: dict[str, Any]) -> None:
    with contextlib.suppress(BrokenPipeError, OSError):
        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))


def _parse_request(line: bytes) -> dict[str, Any]:
    """Decode + validate one request line (raises ValueError/TypeError on
    garbage — both caught by ``_handle_conn``)."""
    req = cast("dict[str, Any]", json.loads(line.decode("utf-8")))
    if not isinstance(req, dict) or not isinstance(req.get("op"), str):
        raise TypeError("request is not an object with a string op")
    return req


def _handle_conn(conn: socket.socket, session: PtySession) -> None:
    with conn:
        conn.settimeout(30.0)
        try:
            line = _read_line(conn)
            if line is None:
                return
            req = _parse_request(line)
        except (ValueError, TypeError, UnicodeDecodeError, OSError, TimeoutError) as exc:
            _respond(conn, err(1, f"bad request: {exc}"))
            return
        handler = _OPS.get(req["op"])
        if handler is None:
            _respond(conn, err(1, f"unknown op {req['op']!r}"))
            return
        try:
            resp = handler(session, req)
        except Exception as exc:  # a handler bug must not wedge the host
            logger.exception("pty host op {op} failed", op=req["op"])
            resp = err(1, f"internal error in {req['op']}: {exc}")
        _respond(conn, resp)


# ---------------------------------------------------------------------------
# Startup.
# ---------------------------------------------------------------------------

# Prompt-wait cap before the initial command is written anyway (a busy CI
# box can init an interactive shell slowly); then a settle beat.
_INITIAL_CMD_READY_TIMEOUT_S = 30.0
_INITIAL_CMD_SETTLE_S = 0.3


def _schedule_initial_command(session: PtySession, cmd: str) -> None:
    """Submit the session's initial command once its login shell is READY.

    A pre-ready write is flushed by the shell's own tcsetattr(TCSAFLUSH)
    during interactive init — the observed loss under a slow CI login shell.
    Readiness is the prompt: bash prints it only after setting its terminal
    modes. The wait runs on a BACKGROUND thread so a slow login shell cannot
    block the request loop. (This builds the pyte screen — an initial-command
    session pays the import; a plain interactive shell does not.)
    """

    def _submit() -> None:
        try:
            deadline = time.monotonic() + _INITIAL_CMD_READY_TIMEOUT_S
            while time.monotonic() < deadline:
                if session.dead:
                    return
                prompt = session.screen().current_line()
                if prompt.endswith(("$", "#")):
                    break
                time.sleep(0.1)
            time.sleep(_INITIAL_CMD_SETTLE_S)
            os.write(session.master_fd, cmd.encode() + b"\r")
        except OSError:  # fail-fast-ok: a just-dead session must not crash the host — the write is best-effort; a runner that never started is covered by the schedule reconcile/breaker
            pass

    threading.Thread(target=_submit, daemon=True).start()


def _socket_answers(path: Path) -> bool:
    """True when a live host answers an OK ping on `path`.

    Strict: the reply must parse as a successful ping — a dying host answers
    err 3 (see ``_op_ping``) and must NOT count as an owner, and random bytes
    from something else on the path must not either (P2 review).
    """
    with (
        contextlib.suppress(OSError, ValueError),
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe,
    ):
        probe.settimeout(2.0)
        probe.connect(str(path))
        probe.sendall(b'{"op": "ping"}\n')
        raw = probe.recv(65536)
        resp = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
        if not isinstance(resp, dict):
            return False
        return bool(cast("dict[str, Any]", resp).get("ok"))
    return False


def _bind_session_socket(sock_file: Path, name: str) -> socket.socket | None:
    """Bind the session socket, bind-first (P2 TOCTOU review).

    Try the bind before any probe: two concurrent spawns then serialize on
    the kernel's EADDRINUSE instead of racing a probe→unlink→bind window
    (where each could unlink the other's freshly bound socket). Only the
    loser of the bind probes the path — a live answer means the name is
    genuinely owned; a dead one means a stale file from a crashed host,
    unlinked and re-bound (one retry: a second EADDRINUSE means a live race
    winner took it meanwhile).
    """
    for attempt in (0, 1):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(sock_file))
            sock_file.chmod(0o600)
            server.listen(32)
        except OSError as exc:
            server.close()
            if exc.errno != errno.EADDRINUSE or attempt == 1:
                sys.stderr.write(f"cannot bind session socket {sock_file}: {exc}\n")
                return None
            if _socket_answers(sock_file):
                sys.stderr.write(f"a live host already owns session {name!r}\n")
                return None
            with contextlib.suppress(OSError):
                sock_file.unlink()  # stale socket from a crashed host
            continue
        return server
    return None


def _fork_shell(cwd: str, env: dict[str, str], cols: int, rows: int) -> tuple[int, int]:
    """pty.fork the login shell; returns (pid, master_fd). Child never returns."""
    pid, master = pty.fork()
    if pid == 0:  # child: the login shell (the pane shape)
        try:
            _set_winsz(0, cols, rows)  # fd 0 = the pty slave
            os.chdir(cwd)
            # The host inherits the CREATING AGENT's env (it is spawned from
            # the agent's process, via _reparent). A service-profile marker
            # must still never leak into a shell child: `import ava` under a
            # runner profile cannot construct the agent domain (Task #856
            # fail-fast) and every watcher would die at boot. Dropped BEFORE
            # the envfile overlay so an explicit caller-supplied marker rides.
            os.environ.pop("AVA_PROCESS_PROFILE", None)
            os.environ.update(env)  # envfile overlay, never argv
            os.environ.setdefault("TERM", "xterm-256color")
            os.environ.setdefault("LANG", "en_US.UTF-8")
            os.execvp("/bin/bash", ["/bin/bash", "-l", "-i"])  # noqa: S606 — the pty child execs the login shell directly; a wrapper would defeat the pty
        except BaseException:
            os._exit(127)
    return pid, master


def main(argv: list[str] | None = None) -> int:
    """Bring up one session, serve it until it dies."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (7, 8):
        sys.stderr.write(
            "usage: pty_sessions.host <name> <cwd> <envfile> <record> <socket> <transcript> "
            "<generation> [cmd_b64]\n"
        )
        return 2
    name, cwd, envfile = args[0], args[1], args[2]
    rec_path, sock_file, transcript = Path(args[3]), Path(args[4]), Path(args[5])
    generation = args[6] or None
    cmd_b64 = args[7] if len(args) == 8 else ""

    # A stray terminal hangup or a TERM aimed at the shell's tree must not
    # take the session down; ending a session is the kill op's job.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    # stderr lands in $AVA_HOME/logs/<name>.host.log (the _reparent
    # redirect); no DB/file sinks — a session host must keep running when
    # the cluster's data plane is down, and never pays a settings build.
    logger.add(
        sys.stderr,
        format="{time:HH:mm:ss.SSS} <level>{level: <5}</level> {message}",
        level="INFO",
        colorize=False,
    )

    if not _NAME_RE.fullmatch(name):
        sys.stderr.write(f"invalid session name {name!r} — use a lowercase slug\n")
        return 2
    if not Path(cwd).is_dir():
        sys.stderr.write(f"cwd is not a directory: {cwd}\n")
        return 1
    try:
        env = _load_env_file(envfile)
    except OSError as exc:
        sys.stderr.write(f"cannot read envfile {envfile}: {exc}\n")
        return 1
    finally:
        # The envfile's only job was the handoff; consume it on every path so
        # a host that never starts leaves no 0600 file (possible tokens)
        # behind until the stale sweep.
        with contextlib.suppress(OSError):
            Path(envfile).unlink()
    cmd = base64.b64decode(cmd_b64.encode("ascii")).decode() if cmd_b64 else None

    brought = _bring_up(name, cwd, env, cmd, rec_path, sock_file, transcript, generation)
    if isinstance(brought, int):
        return brought
    server, session = brought
    while True:
        try:
            conn, _addr = server.accept()
        except OSError:
            continue
        threading.Thread(target=_handle_conn, args=(conn, session), daemon=True).start()


def _bring_up(
    name: str,
    cwd: str,
    env: dict[str, str],
    cmd: str | None,
    rec_path: Path,
    sock_file: Path,
    transcript: Path,
    generation: str | None,
) -> tuple[socket.socket, PtySession] | int:
    """Bind the session socket, fork the shell, persist the record, start the
    reader. Returns (server, session), or an exit code on failure."""
    server = _bind_session_socket(sock_file, name)
    if server is None:
        return 1
    cols, rows = DEFAULT_COLS, DEFAULT_ROWS
    try:
        pid, master = _fork_shell(cwd, env, cols, rows)
    except OSError as exc:
        # EAGAIN = the box hit kern.tty.ptmx_max (511 on macOS) — fail the
        # create cleanly; the spawner reports this log.
        sys.stderr.write(f"cannot allocate pty for {name}: {exc}\n")
        with contextlib.suppress(OSError):
            sock_file.unlink()
        return 1
    _set_winsz(master, cols, rows)
    try:
        create_time = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        create_time = _DEAD_CHILD_SENTINEL
    starttime = None if create_time == _DEAD_CHILD_SENTINEL else pid_starttime_ticks(pid)
    now = time.time()
    record = SessionRecord(pid, create_time, "/bin/bash -l -i", cwd, now, starttime, generation)
    session = PtySession(name, pid, master, cols, rows, record, rec_path, transcript)
    write_record(
        rec_path,
        record,
        host_pid=os.getpid(),
        host_create_time=_own_create_time(),
        host_starttime=pid_starttime_ticks(os.getpid()),
    )
    threading.Thread(target=_reader_loop, args=(session, sock_file), daemon=True).start()
    logger.info(
        "pty session started: {name} (pid={pid}, host={host})", name=name, pid=pid, host=os.getpid()
    )
    if cmd is not None:
        _schedule_initial_command(session, cmd)
    return server, session


def _own_create_time() -> float:
    try:
        return psutil.Process(os.getpid()).create_time()
    except psutil.Error:  # fail-fast-ok: identity extras degrade, liveness key is the shell
        return 0.0


if __name__ == "__main__":
    raise SystemExit(main())
