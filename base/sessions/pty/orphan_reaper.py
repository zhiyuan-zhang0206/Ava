"""Force-reap recordless/duplicate PTY session hosts.

A detached host that loses its record — or is replaced by a later ``new`` that
re-wrote the registry while the older host survived — never exits on its own:
hosts deliberately ignore SIGTERM, and the session layer's recovery paths key on
the on-disk record. This module is the narrow SIGKILL-only escape hatch, called
by ``cli.py`` before ``new`` and after ``kill`` / record-based kill. Its
authorization boundary is argv matching (exact module invocation + session name
+ resolved record path) plus record identity (pid + start-time, with pid-reuse
safety), so it never reaps a process it cannot positively bind to this session
namespace.
"""

from __future__ import annotations

import contextlib
import json
import socket
import time
from pathlib import Path
from typing import Any, cast

import psutil

from base.log import logger
from base.native_process import pid_starttime_ticks
from base.native_process.ownership import stable_create_time
from base.sessions.pty._paths import (
    host_identity,
    host_starttime,
    record_path,
    socket_path,
)
from base.sessions.pty.session_tree import kill_host_tree

# Record-owner identity tolerance, module-scoped to the reaper (the same value
# cli.py uses for its own record checks; deliberately not shared to keep this
# module import-free of its caller).
_RECORD_OWNER_TOLERANCE_S = 2.0
# A host writes its record before it begins accepting requests, so an answering
# socket plus no record is conclusive evidence of an orphan. An unresponsive
# host could still be in that short startup window; `new` leaves it alone for
# this long, while an explicit `kill` may force-reap it immediately.
# Local, non-lattice timeouts (deliberately NOT in base/deploy/timing.py: no
# ordering relation with any registered clock).
_ORPHAN_HOST_STARTUP_LEEWAY_S = 5.0
_ORPHAN_HOST_KILL_WAIT_S = 3.0
_PTY_HOST_MODULE = "base.sessions.pty.host"


def _host_answers_ping(name: str) -> bool:
    """Whether the named host's socket answers a short successful ping.

    This deliberately does not use `session_request`: a host whose request
    loop is wedged must not make an orphan sweep wait the normal 30 seconds.
    """
    with (
        contextlib.suppress(OSError, ValueError),
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn,
    ):
        conn.settimeout(0.2)
        conn.connect(str(socket_path(name)))
        conn.sendall(b'{"op":"ping"}\n')
        raw = conn.recv(65536)
        resp = cast("dict[str, Any]", json.loads(raw.split(b"\n", 1)[0].decode("utf-8")))
        return bool(resp.get("ok"))
    return False


def _named_host_processes(name: str) -> list[psutil.Process]:
    """Live PTY hosts whose argv binds them to this name and record path.

    Matching both the module invocation and the pre-resolved record path keeps
    this recovery path scoped to a host created for *this* session namespace;
    it never treats an arbitrary process with a similar display name as ours.
    """
    expected_record = record_path(name).resolve()
    hosts: list[psutil.Process] = []
    for proc in psutil.process_iter(["cmdline"]):
        try:
            raw_cmdline = proc.info.get("cmdline")
            if not isinstance(raw_cmdline, list):
                continue
            cmdline = cast("list[str]", raw_cmdline)
            for index, token in enumerate(cmdline):
                if (
                    token == _PTY_HOST_MODULE
                    and index >= 1
                    and cmdline[index - 1] == "-m"
                    and len(cmdline) > index + 4
                    and cmdline[index + 1] == name
                    and Path(cmdline[index + 4]).expanduser().resolve() == expected_record
                ):
                    hosts.append(proc)
                    break
        except (psutil.Error, OSError):
            continue
    return hosts


def _record_owns_host(host: psutil.Process, name: str) -> bool:
    """Whether the current record identifies `host`, including PID reuse safety."""
    identity = host_identity(record_path(name))
    if identity is None or host.pid != identity[0]:
        return False
    try:
        recorded_starttime = host_starttime(record_path(name))
        if recorded_starttime is not None:
            return pid_starttime_ticks(host.pid) == recorded_starttime
        return abs(stable_create_time(host) - identity[1]) <= _RECORD_OWNER_TOLERANCE_S
    except psutil.Error:
        return False


def _orphaned_host_processes(
    name: str, *, force_unresponsive: bool = False
) -> list[psutil.Process]:
    """The duplicate/recordless PTY hosts safe to force-reap for `name`.

    With a sound record, retain exactly its identity and reap only extra hosts.
    With no record, an answering socket proves the host already passed record
    creation and then lost it. An explicit `kill` may additionally reap an
    unresponsive matching host immediately; `new` gives a possible startup five
    seconds before considering it stale.
    """
    hosts = _named_host_processes(name)
    if not hosts:
        return []
    path = record_path(name)
    if path.exists():
        # An unreadable/legacy record cannot safely nominate an owner, so leave
        # it untouched rather than risking its live host.
        owners = [host for host in hosts if _record_owns_host(host, name)]
        if not owners:
            return []
        return [host for host in hosts if host not in owners]
    if force_unresponsive or _host_answers_ping(name):
        return hosts
    now = time.time()
    orphaned: list[psutil.Process] = []
    for host in hosts:
        try:
            if now - host.create_time() >= _ORPHAN_HOST_STARTUP_LEEWAY_S:
                orphaned.append(host)
        except psutil.Error:
            continue
    return orphaned


def _reap_orphaned_hosts(name: str, *, force_unresponsive: bool = False) -> int:
    """Force-reap duplicate or recordless hosts for `name`, returning host count.

    A PTY host intentionally ignores SIGTERM, so this is a deliberately narrow
    SIGKILL-only escape hatch. Its argv and record-identity checks above are the
    authorization boundary. Each host is frozen and its shell's whole session
    (`session_tree`: the shell's tree and POSIX session) dies before the host,
    so schedule runners cannot escape into their own process groups. Only a
    survivor this user could signal fails the reap (`TreeKill.stuck`); one it
    may not signal (a root `sudo` on the pty) is logged and left to its owner.
    """
    hosts = _orphaned_host_processes(name, force_unresponsive=force_unresponsive)
    if not hosts:
        return 0
    logger.warning(
        "pty force-reaping orphan hosts for {name}: pids={pids}",
        name=name,
        pids=sorted(host.pid for host in hosts),
    )
    stuck: list[int] = []
    denied: list[int] = []
    for host in hosts:
        result = kill_host_tree(host, wait_s=_ORPHAN_HOST_KILL_WAIT_S)
        stuck += [identity.pid for identity in result.stuck]
        denied += [identity.pid for identity in result.denied]
    if stuck:
        raise RuntimeError(f"recordless pty host {name} survived force-reap: pids={sorted(stuck)}")
    if denied:
        logger.warning(
            "pty force-reap of {name} left processes this user may not signal: pids={pids}",
            name=name,
            pids=sorted(denied),
        )
    return len(hosts)
