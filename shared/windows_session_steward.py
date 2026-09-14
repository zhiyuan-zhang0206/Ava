"""Resident per-session control steward — the cross-session control channel.

`AttachConsole` cannot cross a session boundary, so a graceful-stop caller in a
different session than the target cannot deliver Ctrl-Break directly. This leaf
solves the problem at *launch* time instead of control time: `winproc.new_session`
spawns it in the same session the target runs in (the spawner's own session),
and it serves one loopback TCP control socket whose endpoint and delivery
token live only in the session record (and this process's argv). Loopback TCP,
not a filesystem socket: Windows CPython has no AF_UNIX (CPython issue
#77589), so the channel must use the one stream family the platform
implements.

A `break` request makes it execute the existing one-shot private-console helper
(`shared/windows_console_signal.py`) — every identity and console-membership
check stays in that leaf, unchanged. Successful delivery means Windows accepted
a request, not that the target exited.

Invoked by absolute loaded-image file path under isolated Python (`-I`). It does
not import Ava settings, inspect cwd, or load code from PYTHONPATH. Stdout/stderr
go to the session's control log (identity/lifecycle metadata only — no
environment, credentials, profile, or message bodies).

Lifetime: exits once its (pid, birth) is no longer the live target the record
names — after a graceful stop, a force kill, a record reap, or a same-name
restart. It is a sibling of the target, never part of its tree, so a tree kill
never reaches it; recorded stewards are spared by the kill boundary the same way
recorded sessions are.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil

_HELPER = Path(__file__).resolve().with_name("windows_console_signal.py")
_HELPER_DEADLINE_S = 10.0
_HELPER_TIMEOUT_S = 15.0
_POLL_S = 3.0
_MAX_REQUEST_BYTES = 64


def _log(message: str) -> None:
    # stdout is the session's control log (winproc redirects it there): the
    # only deliberate print in this leaf, and the only lifecycle/identity line.
    print(f"[steward] {message}", flush=True)  # noqa: T201


def _record_matches(record_path: Path, pid: int, birth: float) -> bool:
    try:
        record = json.loads(record_path.read_text())
    except (ValueError, OSError):
        return False
    return (record.get("pid"), record.get("create_time")) == (pid, birth)


def _target_alive(pid: int, birth: float) -> bool:
    try:
        proc = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return proc.is_running() and proc.create_time() == birth


def should_exit(*, record_names_me: bool, target_alive: bool, record_exists: bool) -> bool:
    """Whether a steward serving one exact (pid, birth) identity must leave.

    Serve only while the record names this identity and the target lives. An
    absent record is tolerated while the target lives (the spawn race: the
    steward starts before the record write lands); a record naming a different
    identity always ends this steward (same-name restart or reap), as does a
    dead target.
    """
    if record_names_me:
        return not target_alive
    if record_exists:
        return True
    return not target_alive


def _deliver_break(record_path: Path, pid: int, birth: float) -> tuple[bool, str]:
    """Run the verified private-console helper; return (accepted, detail)."""
    deadline = time.monotonic() + _HELPER_DEADLINE_S
    try:
        result = subprocess.run(  # noqa: S603 — fixed leaf argv, repo-internal literals
            [
                sys.executable,
                "-I",
                str(_HELPER),
                str(record_path),
                str(pid),
                str(birth),
                str(deadline),
            ],
            capture_output=True,
            text=True,
            timeout=_HELPER_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "private console helper exceeded its deadline"
    if result.returncode == 0:
        return True, "accepted"
    return False, result.stderr.strip().splitlines()[
        -1
    ] if result.stderr.strip() else f"exit {result.returncode}"


def _handle_request(
    connection: socket.socket,
    record_path: Path,
    pid: int,
    birth: float,
    name: str,
    nonce: str,
    *,
    record_names_me: bool,
) -> None:
    """Answer one accepted connection; each exchange is one line in, one out.

    The first line must carry the record's delivery token, so a plain `break`
    (what a squatter or a stale client would send) is refused exactly like an
    unknown request.
    """
    connection.settimeout(_HELPER_TIMEOUT_S + _POLL_S)
    request = connection.recv(_MAX_REQUEST_BYTES)
    if request.strip() != f"break {nonce}".encode():
        connection.sendall(b"err: unknown request or bad token\n")
        return
    if not record_names_me:
        connection.sendall(b"err: session record no longer names this steward\n")
        return
    if not _target_alive(pid, birth):
        connection.sendall(b"err: target exited before delivery\n")
        return
    accepted, detail = _deliver_break(record_path, pid, birth)
    if accepted:
        _log(f"delivered graceful break to {name}")
        connection.sendall(b"ok\n")
    else:
        _log(f"delivery refused for {name}: {detail}")
        connection.sendall(f"err: {detail}\n".encode("utf-8", errors="replace"))


def serve(record_path: Path, pid: int, birth: float, port: int, nonce: str, name: str) -> int:
    """Serve control requests until the session identity is no longer live."""
    if sys.platform != "win32":
        raise RuntimeError("session steward is Windows-only")
    if not _target_alive(pid, birth):
        _log(f"target {pid} is already gone at start; refusing to serve {name}")
        return 1
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(_POLL_S)
    try:
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
    except OSError as error:
        _log(f"cannot bind control port for {name} (127.0.0.1:{port}): {error}")
        return 1
    _log(f"serving control port for {name} (pid {pid}, birth {birth}): 127.0.0.1:{port}")
    try:
        while True:
            # Exit truth table: see should_exit (module docstring for why).
            record_names_me = _record_matches(record_path, pid, birth)
            if should_exit(
                record_names_me=record_names_me,
                target_alive=_target_alive(pid, birth),
                record_exists=record_path.exists(),
            ):
                _log(f"leaving {name}: session identity no longer live")
                return 0
            try:
                connection, _peer = sock.accept()
            except TimeoutError:  # the poll wake-up, not an error
                continue
            try:
                _handle_request(
                    connection,
                    record_path,
                    pid,
                    birth,
                    name,
                    nonce,
                    record_names_me=record_names_me,
                )
            except OSError as error:
                # A local peer that connects and vanishes must not take the
                # control channel down with it (any local process can reach a
                # loopback port; the token check already gated every action).
                _log(f"connection dropped for {name}: {error}")
            finally:
                with contextlib.suppress(OSError):
                    connection.close()
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    return 0


if __name__ == "__main__":
    _record, _pid, _birth, _port, _nonce, _name = sys.argv[1:7]
    raise SystemExit(
        serve(
            Path(_record),
            int(_pid),
            float(_birth),
            int(_port),
            _nonce,
            _name,
        )
    )
