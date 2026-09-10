"""Session launch: socket bind, login-shell fork, record write.

Split out of ``host.py`` (2026-09-10, issue #2063): the host module hit the
800-line hard ceiling when the bring-up gained the pty record lock. This
module owns everything between "the name is validated" and "the session
answers its socket": the bind-first socket protocol, the pty fork, and the
record write under the registry lock.

``host.py`` imports ``_bring_up`` back from here at the bottom of its module;
this module reaches the session model (``PtySession``, the reader loop, the
initial-command scheduler) through the lazy ``_host()`` resolver — the import
happens at call time, when host.py is fully loaded (host.py also registers
itself under its canonical module name so ``python -m`` keeps one instance).
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import pty
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import psutil

from shared.log import logger
from shared.platform import LockTimeoutError, file_lock
from shared.pty_sessions._paths import (
    DEFAULT_COLS,
    DEFAULT_ROWS,
    records_lock_path,
    write_record,
)
from shared.session_record import SessionRecord, pid_starttime_ticks

# Sentinel for a child whose create_time could not be read (died at spawn —
# the pid is at its most reusable moment): can never match a reused pid.
_DEAD_CHILD_SENTINEL = -1.0


def _host() -> Any:
    """The host module, resolved lazily.

    ``host.py`` imports ``_bring_up`` from this module at its bottom, so a
    top-level import back would be a cycle; at call time host.py is fully
    loaded and the attribute lookup is safe.
    """
    from shared.pty_sessions import host

    return host


# A host may wait on a concurrent sweep (bounded by its own 5s timeout); 30s
# leaves generous headroom before the spawner reports the host as failed.
_BRING_UP_LOCK_TIMEOUT_S = 30.0


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
            _host()._set_winsz(0, cols, rows)  # fd 0 = the pty slave
            os.chdir(cwd)
            # Ignored dispositions survive exec — reset them here or the
            # shell's jobs never receive stop's per-job TERM (#2045).
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGPIPE):
                signal.signal(sig, signal.SIG_DFL)
            # The host inherits the CREATING AGENT's env (spawned via
            # _reparent); a service-profile marker must never leak into a
            # shell child (`import ava` under a runner profile fails fast,
            # Task #856). Dropped BEFORE the envfile overlay so an explicit
            # caller-supplied marker rides.
            os.environ.pop("AVA_PROCESS_PROFILE", None)
            os.environ.update(env)  # envfile overlay, never argv
            os.environ.setdefault("TERM", "xterm-256color")
            os.environ.setdefault("LANG", "en_US.UTF-8")
            os.execvp("/bin/bash", ["/bin/bash", "-l", "-i"])  # noqa: S606 — the pty child execs the login shell directly; a wrapper would defeat the pty
        except BaseException:
            os._exit(127)
    return pid, master


def _bring_up(
    name: str,
    cwd: str,
    env: dict[str, str],
    cmd: str | None,
    rec_path: Path,
    sock_file: Path,
    transcript: Path,
    generation: str | None,
) -> (
    tuple[socket.socket, Any] | int
):  # session type = host.PtySession (lazy resolver keeps the cycle out of annotations)
    """Bind the session socket, fork the shell, persist the record, start the
    reader. Returns (server, session), or an exit code on failure.

    Bind + record write run under the pty record lock (issue #2063): a
    concurrent lazy sweep that read the previous incarnation's dead record
    must not unlink this session's fresh record + socket — the lock
    serializes record creation against record sweeping.
    """
    try:
        with file_lock(records_lock_path(), timeout_s=_BRING_UP_LOCK_TIMEOUT_S):
            return _bring_up_locked(
                name, cwd, env, cmd, rec_path, sock_file, transcript, generation
            )
    except LockTimeoutError as exc:
        sys.stderr.write(f"cannot take the pty record lock for {name}: {exc}\n")
        return 1


def _bring_up_locked(
    name: str,
    cwd: str,
    env: dict[str, str],
    cmd: str | None,
    rec_path: Path,
    sock_file: Path,
    transcript: Path,
    generation: str | None,
) -> (
    tuple[socket.socket, Any] | int
):  # session type = host.PtySession (lazy resolver keeps the cycle out of annotations)
    """The bring-up body — caller holds the pty record lock."""
    host = _host()
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
    host._set_winsz(master, cols, rows)
    try:
        create_time = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        create_time = _DEAD_CHILD_SENTINEL
    starttime = None if create_time == _DEAD_CHILD_SENTINEL else pid_starttime_ticks(pid)
    now = time.time()
    record = SessionRecord(pid, create_time, "/bin/bash -l -i", cwd, now, starttime, generation)
    session = host.PtySession(name, pid, master, cols, rows, record, rec_path, transcript)
    write_record(
        rec_path,
        record,
        host_pid=os.getpid(),
        host_create_time=_own_create_time(),
        host_starttime=pid_starttime_ticks(os.getpid()),
    )
    threading.Thread(target=host._reader_loop, args=(session, sock_file), daemon=True).start()
    logger.info(
        "pty session started: {name} (pid={pid}, host={host})", name=name, pid=pid, host=os.getpid()
    )
    if cmd is not None:
        host._schedule_initial_command(session, cmd)
    return server, session


def _own_create_time() -> float:
    try:
        return psutil.Process(os.getpid()).create_time()
    except psutil.Error:  # fail-fast-ok: identity extras degrade, liveness key is the shell
        return 0.0
