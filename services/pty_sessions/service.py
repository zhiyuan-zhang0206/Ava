"""The pty-sessions service: every agent shell on this machine, one ordinary process.

It holds each session's pty master and the in-memory session table, serves a
unix-socket JSON-line protocol (`base.sessions.pty.protocol`) to the clients in
agent, schedule and page-server processes, and is stopped and started with the
rest of the roster. A session therefore outlives an agent, an agent host or a
gateway restarting; it ends with its shell, a `kill`, a `close_all`, or this
service stopping. No database, no plugin: the allocation freeze is a file marker
and the ledger a file (`ledger`).

One event loop does the I/O: it accepts requests, reads every master and feeds
its session's screen, ring and transcript, and ends sessions. Everything that can
block (forking a shell, a kill, a closure, a screen render) is a request handler
run on the executor, so `ping` is answered on the loop and never queues behind
them: the ownership probe times out at three seconds.

Requests are `{"id", "method", ...}`. Methods: `ping`, `has`, `list`, `new`,
`send`, `capture`, `resize`, `kill`, `close_all`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psutil

from base.log import logger
from base.native_process import child_env, pid_starttime_ticks
from base.native_process.ownership import OwnedProcess, stable_create_time
from base.sessions.pty import closure, protocol, session_tree
from base.sessions.pty.allocation_freeze import locked_freeze_state, state_path
from base.sessions.pty.paths import (
    CAPTURE_MAX_LINES,
    DEFAULT_COLS,
    DEFAULT_ROWS,
    RESIZE_MAX,
    ledger_path,
    transcript_path,
)
from base.sessions.record import SessionRecord
from services.pty_sessions import ledger, session

# Session names ride the transcript filename; keep the conservative slug shape
# ava/shell/sessions.py enforces for its names.
_NAME_RE = re.compile(r"[a-z][a-z0-9-]*")

# Reading one request must not hold a connection forever.
_REQUEST_READ_TIMEOUT_S = 30.0

# How long `close_all` waits, after the closure, for the event loop to drop the
# sessions the closure ended from the table.
_DRAIN_WAIT_S = 3.0

# How often the loop looks for a shell that exited without closing its pty (a
# background child still holds the slave, so no EOF reaches the master).
_EXIT_CHECK_S = 0.5

# Requests that block (a fork, a kill that waits for its members) each hold an
# executor thread: far more than the machine's sessions could ever need at once.
_OP_THREADS = 256

_LISTEN_BACKLOG = 128


def _still(identity: OwnedProcess) -> bool:
    try:
        return identity.live()
    except RuntimeError:
        return True  # unverifiable: keep it on the ledger, never certify it gone


class RequestError(Exception):
    """A request the service refuses: becomes an `err` response."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _text(req: dict[str, Any], key: str) -> str:
    value = req.get(key)
    if not isinstance(value, str) or not value:
        raise RequestError(protocol.BAD_REQUEST, f"{key} must be a non-empty string")
    return value


def _count(req: dict[str, Any], key: str, default: int, *, low: int, high: int) -> int:
    value = req.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RequestError(protocol.BAD_REQUEST, f"{key} must be an integer")
    if not low <= value <= high:
        raise RequestError(protocol.BAD_REQUEST, f"{key} out of range: {value}")
    return value


def _seconds(req: dict[str, Any], key: str) -> float:
    value = req.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise RequestError(protocol.BAD_REQUEST, f"{key} must be a non-negative number")
    return float(value)


def _child_env(overlay: object) -> dict[str, str]:
    """The shell's environment: this service's, minus its own markers, then the caller's.

    The service environment is the base (the creator's own variables, an
    `SSH_AUTH_SOCK`, are not carried; the caller forwards what a shell needs);
    a service-profile marker must never leak into a shell child (`import ava`
    under a runner profile fails fast), nor an activated virtualenv the caller
    did not ask for. An explicit overlay entry for either wins.
    """
    if not isinstance(overlay, dict):
        raise RequestError(protocol.BAD_REQUEST, "env must be an object of strings")
    env = child_env.inherited_process_env()
    env.pop("AVA_PROCESS_PROFILE", None)
    env.pop("VIRTUAL_ENV", None)
    for key, value in overlay.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RequestError(protocol.BAD_REQUEST, "env must be an object of strings")
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise RequestError(protocol.BAD_REQUEST, f"env entry {key!r} cannot be forwarded")
        env[key] = value
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("LANG", "en_US.UTF-8")
    return env


class PtyService:
    """The session table and the request handlers; `serve` runs them over a socket."""

    def __init__(self) -> None:
        self._sessions: dict[str, session.PtySession] = {}
        self._lock = threading.Lock()
        self._closing = False
        # Set once `serve` runs: the loop and its task group, and the stop request.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: asyncio.TaskGroup | None = None
        self._stop: asyncio.Event | None = None
        self._finished = False
        # What the start-time sweep closed: the busy sessions a caller turns into owner notices.
        self.swept = closure.Outcome()
        self._methods: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {
            "ping": self._ping,
            "has": self._has,
            "list": self._list,
            "new": self._new,
            "send": self._send,
            "capture": self._capture,
            "resize": self._resize,
            "kill": self._kill,
            "close_all": self._close_all,
        }

    # -- table ---------------------------------------------------------------

    def _live(self, name: str) -> session.PtySession | None:
        """The named session while its shell runs; None otherwise."""
        with self._lock:
            found = self._sessions.get(name)
        if found is None or found.dead or not found.pid_matches():
            return None
        return found

    def _require(self, req: dict[str, Any]) -> session.PtySession:
        name = _text(req, "name")
        found = self._live(name)
        if found is None:
            raise RequestError(protocol.NO_SUCH_SESSION, f"no such pty session: {name}")
        return found

    def _persist(self) -> None:
        """Rewrite the ledger from the table; caller holds the table lock."""
        targets = [
            closure.Target(name, s.shell, tuple(m for m in s.capture.members if m != s.shell))
            for name, s in self._sessions.items()
        ]
        try:
            ledger.write(ledger_path(), targets)
        except OSError as exc:
            logger.warning("pty ledger write failed: {exc}", exc=exc)

    def snapshot_members(self) -> None:
        """Fold every session's current membership into the ledger.

        One process-table scan serves every session (`session_tree.refresh`);
        members that exited are dropped so a long-lived shell's capture does not
        grow with every command it ever ran.
        """
        with self._lock:
            live = list(self._sessions.values())
        captures = [s.capture for s in live]
        session_tree.refresh(captures)
        for s in live:
            s.capture.members = [m for m in s.capture.members if m == s.shell or _still(m)]
        with self._lock:
            self._persist()

    def _ended(self, ended: session.PtySession) -> None:
        with self._lock:
            if self._sessions.get(ended.name) is ended:
                del self._sessions[ended.name]
                self._persist()

    # -- handlers ------------------------------------------------------------

    def _ping(self, req: dict[str, Any]) -> dict[str, Any]:
        del req
        return {"pid": os.getpid()}

    def _has(self, req: dict[str, Any]) -> dict[str, Any]:
        return {"alive": self._live(_text(req, "name")) is not None}

    def _list(self, req: dict[str, Any]) -> dict[str, Any]:
        prefix = req.get("prefix", "")
        if not isinstance(prefix, str):
            raise RequestError(protocol.BAD_REQUEST, "prefix must be a string")
        with self._lock:
            candidates = [s for n, s in sorted(self._sessions.items()) if n.startswith(prefix)]
        rows = [
            {
                "name": s.name,
                "pid": s.pid,
                "create_time": s.record.create_time,
                "starttime": s.record.starttime,
                "cmd": s.record.cmd,
                "cwd": s.record.cwd,
                "started_at": s.record.started_at,
                "generation": s.record.generation,
            }
            for s in candidates
            if not s.dead and s.pid_matches()
        ]
        return {"sessions": rows}

    def _new(self, req: dict[str, Any]) -> dict[str, Any]:
        name = _text(req, "name")
        if not _NAME_RE.fullmatch(name):
            raise RequestError(
                protocol.BAD_REQUEST, f"invalid session name {name!r}: use a lowercase slug"
            )
        cwd = _text(req, "cwd")
        if not Path(cwd).is_dir():
            raise RequestError(protocol.ERROR, f"cwd is not a directory: {cwd}")
        cmd = req.get("cmd")
        if cmd is not None and not isinstance(cmd, str):
            raise RequestError(protocol.BAD_REQUEST, "cmd must be a string")
        env = _child_env(req.get("env", {}))
        # The allocation lock is held until the session is registered: once a
        # concurrent freeze returns, every earlier allocation is visible here
        # and every later one observes the marker.
        with locked_freeze_state() as freeze:
            existing = self._live(name)
            if existing is not None:
                if (
                    freeze.status != "frozen"
                    and freeze.generation is not None
                    and existing.record.generation != freeze.generation
                ):
                    raise RequestError(
                        protocol.ERROR,
                        f"pty session {name} belongs to a prior generation; reap it before "
                        "rebuilding desired state",
                    )
                # Already exists = idempotent no-op, including while frozen. A
                # freeze protects absent -> live allocation, never use of an
                # existing session.
                return {"pid": existing.pid, "created": False}
            if self._closing:
                raise RequestError(protocol.ERROR, "pty allocation refused: sessions are closing")
            if freeze.status == "frozen":
                raise RequestError(
                    protocol.ERROR,
                    "pty allocation refused: host allocation is frozen by "
                    f"{freeze.holder!r} (generation {freeze.generation!r}): {freeze.reason}",
                )
            if freeze.status == "invalid":
                raise RequestError(
                    protocol.ERROR,
                    f"pty allocation refused: host freeze marker {state_path()} is invalid "
                    f"({freeze.error}); repair it before allocating new sessions",
                )
            created = self._spawn(name, cwd, env, cmd, freeze.generation)
        return {"pid": created.pid, "created": True}

    def _spawn(
        self, name: str, cwd: str, env: dict[str, str], cmd: str | None, generation: str | None
    ) -> session.PtySession:
        cols, rows = DEFAULT_COLS, DEFAULT_ROWS
        try:
            pid, master = session.fork_shell(cwd, env, cols, rows)
        except OSError as exc:
            # EAGAIN = the box hit kern.tty.ptmx_max (511 on macOS): fail the create cleanly.
            raise RequestError(protocol.ERROR, f"cannot allocate pty for {name}: {exc}") from exc
        try:
            session.set_winsz(master, cols, rows)
            try:
                create_time = stable_create_time(psutil.Process(pid))
            except psutil.NoSuchProcess:
                create_time = session.DEAD_CHILD_SENTINEL
            starttime = (
                None if create_time == session.DEAD_CHILD_SENTINEL else pid_starttime_ticks(pid)
            )
            record = SessionRecord(
                pid, create_time, session.SHELL_COMMAND, cwd, time.time(), starttime, generation
            )
            created = session.PtySession(
                name, pid, master, cols, rows, record, transcript_path(name)
            )
        except BaseException:
            # The shell exists but no session owns it: end it rather than orphan it.
            with contextlib.suppress(ProcessLookupError, OSError):
                os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(OSError):
                os.close(master)
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pid, 0)
            raise
        with self._lock:
            self._sessions[name] = created
            self._persist()
        logger.info("pty session started: {name} (pid={pid})", name=name, pid=pid)
        assert self._loop is not None  # noqa: S101 — requests only arrive once `serve` runs
        self._loop.call_soon_threadsafe(self._attach, created, cmd)
        return created

    def _send(self, req: dict[str, Any]) -> None:
        found = self._require(req)
        try:
            data = session.decode_data(req.get("data"))
        except (ValueError, TypeError):
            raise RequestError(protocol.BAD_REQUEST, "send requires base64 text as data") from None
        try:
            found.write(data)
        except OSError as exc:
            raise RequestError(protocol.ERROR, f"session {found.name} is gone: {exc}") from exc

    def _capture(self, req: dict[str, Any]) -> dict[str, Any]:
        found = self._require(req)
        # Protective clamp: CAPTURE_MAX_LINES (why it is a constant lives there).
        lines = _count(req, "lines", 200, low=1, high=CAPTURE_MAX_LINES)
        text = found.screen().render(lines, scrollback=bool(req.get("scrollback", True)))
        return {"text": text}

    def _resize(self, req: dict[str, Any]) -> None:
        found = self._require(req)
        cols = _count(req, "cols", 0, low=1, high=RESIZE_MAX)
        rows = _count(req, "rows", 0, low=1, high=RESIZE_MAX)
        found.resize(cols, rows)

    def _kill(self, req: dict[str, Any]) -> dict[str, Any]:
        found = self._live(_text(req, "name"))
        if found is None:
            return {"mode": "noop", "interrupted": False}  # idempotent, like posixproc
        try:
            return session.kill_session(found, graceful=bool(req.get("graceful", False)))
        except RuntimeError as exc:
            raise RequestError(protocol.ERROR, str(exc)) from exc

    def _close_all(self, req: dict[str, Any]) -> dict[str, Any]:
        grace_s, kill_s = _seconds(req, "grace_s"), _seconds(req, "kill_s")
        return self.close_everything(grace_s=grace_s, kill_s=kill_s).to_wire()

    # -- closure -------------------------------------------------------------

    def close_everything(self, *, grace_s: float, kill_s: float) -> closure.Outcome:
        """Refuse new sessions, close every live one, wait for the table to drain.

        New allocations are refused for the closure's duration; the caller
        (a stop) verifies afterwards that nothing was born meanwhile.
        """
        with self._lock:
            self._closing = True
            targets = [closure.Target(n, s.shell) for n, s in sorted(self._sessions.items())]
        try:
            outcome = closure.close_sessions(targets, grace_s=grace_s, kill_s=kill_s)
            deadline = time.monotonic() + _DRAIN_WAIT_S
            while time.monotonic() < deadline:
                with self._lock:
                    if not self._sessions:
                        break
                time.sleep(0.02)
        finally:
            with self._lock:
                self._closing = False
        return outcome

    # -- the event loop ------------------------------------------------------
    #
    # Everything below runs on the loop thread.

    def _attach(self, created: session.PtySession, cmd: str | None) -> None:
        """Start reading a new session's master; drive its initial command, if any."""
        assert self._loop is not None  # noqa: S101
        self._loop.add_reader(created.master_fd, self._on_readable, created)
        if cmd is not None:
            created.initial = session.InitialCommand(created, cmd)
            self._tick_initial(created)

    def _tick_initial(self, created: session.PtySession) -> None:
        pending = created.initial
        if pending is None or pending.done or created.ending:
            return
        pending.step()
        if not pending.done:
            assert self._loop is not None  # noqa: S101
            self._loop.call_later(session.InitialCommand.TICK_S, self._tick_initial, created)

    def _on_readable(self, created: session.PtySession) -> None:
        try:
            data = os.read(created.master_fd, created.read_size())
        except OSError as exc:
            logger.debug("pty {name}: read ended: {exc}", name=created.name, exc=exc)
            self._end(created)
            return
        if not data:
            self._end(created)  # EOF on the master: the slave side is gone
            return
        created.feed(data)
        created.log_write(data)

    def _end(self, ended: session.PtySession) -> None:
        """Stop reading `ended` and tear it down, once.

        The reader comes off the loop before anything closes the master: a
        descriptor number the kernel hands to the next session's master must
        never find this session's registration still in the loop's table.
        """
        if ended.ending:
            return
        ended.ending = True
        assert self._loop is not None and self._tasks is not None  # noqa: S101
        self._loop.remove_reader(ended.master_fd)
        self._tasks.create_task(self._finish(ended))

    async def _finish(self, ended: session.PtySession) -> None:
        try:
            await asyncio.to_thread(session.finish, ended, self._ended)
        except Exception:  # one session's teardown must not take the service down
            logger.exception("pty session {name} teardown failed", name=ended.name)

    async def _watch_exits(self) -> None:
        """End a session whose shell exited without the master reaching EOF."""
        while not self._finished:
            await asyncio.sleep(_EXIT_CHECK_S)
            with self._lock:
                watched = [s for s in self._sessions.values() if not s.ending]
            for watching in watched:
                try:
                    reaped, _status = os.waitpid(watching.pid, os.WNOHANG)
                except ChildProcessError:
                    reaped = watching.pid  # reaped elsewhere (a teardown): it is gone
                if reaped:
                    self._end(watching)

    async def _snapshot_loop(self) -> None:
        """Snapshot every session's membership into the ledger every `ledger.SNAPSHOT_INTERVAL_S`."""
        assert self._stop is not None  # noqa: S101
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), ledger.SNAPSHOT_INTERVAL_S)
                return
            try:
                await asyncio.to_thread(self.snapshot_members)
            except Exception:  # a failed snapshot only ages the ledger
                logger.exception("pty membership snapshot failed")

    def handle(self, req: dict[str, Any]) -> dict[str, Any]:
        """Answer one request object; never raises."""
        req_id = req.get("id")
        method = req.get("method")
        handler = self._methods.get(method) if isinstance(method, str) else None
        if handler is None:
            return protocol.err(req_id, protocol.BAD_REQUEST, f"unknown method {method!r}")
        try:
            return protocol.ok(req_id, handler(req))
        except RequestError as exc:
            return protocol.err(req_id, exc.code, exc.message)
        except Exception as exc:  # a handler bug must not wedge the service
            logger.exception("pty service method {method} failed", method=method)
            return protocol.err(req_id, protocol.ERROR, f"internal error in {method}: {exc}")

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer the one request on a connection, then close it."""
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), _REQUEST_READ_TIMEOUT_S)
                if not line:
                    return
                req = protocol.decode_object(line)
            except (ValueError, TypeError, UnicodeDecodeError, TimeoutError, OSError) as exc:
                response = protocol.err(None, protocol.ERROR, f"bad request: {exc}")
            else:
                if req.get("method") == "ping":
                    response = self.handle(req)  # on the loop: never queued behind a blocked op
                else:
                    response = await asyncio.get_running_loop().run_in_executor(
                        None, self.handle, req
                    )
            writer.write(protocol.encode(response))
            with contextlib.suppress(OSError):
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def serve(self, path: Path, *, hangup_wait_s: float, kill_s: float) -> None:
        """Serve on `path` until SIGTERM or SIGINT, then close every session still alive.

        The caller holds the home's instance lock, so a socket file already at
        `path` belongs to a service that is gone: it is replaced.
        """
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._stop = asyncio.Event()
        loop.set_default_executor(ThreadPoolExecutor(_OP_THREADS, thread_name_prefix="pty-op"))
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._request_stop, sig)
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            self._connection,
            path=str(path),
            limit=protocol.MAX_REQUEST_BYTES,
            backlog=_LISTEN_BACKLOG,
        )
        path.chmod(0o600)
        bound = path.stat().st_ino
        logger.info("pty-sessions serving {path}", path=path)
        async with asyncio.TaskGroup() as tasks:
            self._tasks = tasks
            tasks.create_task(self._watch_exits())
            tasks.create_task(self._snapshot_loop())
            await self._stop.wait()
            server.close()
            with contextlib.suppress(OSError):
                # Never unlink a later occupant's socket: only the inode this process bound.
                if path.stat().st_ino == bound:
                    path.unlink()
            await asyncio.to_thread(self.close_everything, grace_s=hangup_wait_s, kill_s=kill_s)
            self._finished = True

    def _request_stop(self, signum: int) -> None:
        logger.info("pty-sessions stopping on signal {signum}", signum=signum)
        assert self._stop is not None  # noqa: S101
        self._stop.set()
