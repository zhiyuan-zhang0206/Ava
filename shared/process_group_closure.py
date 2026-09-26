"""Standard-library closure of a process group whose leader this process launched.

A bare interpreter can import this module (no psutil, no pydantic), so the
checkout-retiring runtime proof and the release-store contract use it too.

The direct child launched with `process_group=0` leads its group and stays
UNREAPED until closure is proven: its zombie keeps the group number reserved, so
every `killpg` reaches only this launch. Its exit is observed without reaping
(kqueue NOTE_EXIT on macOS, `waitid(WNOWAIT)` on Linux). Closure is a group
listing that names nothing but the leader, read after a group-wide SIGKILL:
XNU's `proc_listpids(PROC_PGRP_ONLY)` snapshot on macOS, a `/proc` scan on
Linux, where a fork racing the group SIGKILL either fails or hands the pending
signal to the new child. Any other listed member, live or zombie, forces another
round. Only then is the leader reaped. An unresolved closure leaves it unreaped.
"""

from __future__ import annotations

import ctypes
import errno
import os
import select
import signal
import subprocess
import sys
import threading
import time
from functools import cache
from pathlib import Path
from typing import Any, NoReturn

_PROC_PGRP_ONLY = 2  # <sys/proc_info.h>: list PIDs by process-group id.
_POLL_S = 0.05

# Leaders whose closure is unresolved. Holding them keeps Popen's finalizer from
# queueing them for a later reap, which would release their group numbers.
_UNRESOLVED: list[subprocess.Popen[bytes]] = []
_UNRESOLVED_LOCK = threading.Lock()


class GroupClosureUnresolvedError(RuntimeError):
    """Closure was not proven; the leader stays unreaped and keeps its group."""


def wait_leader_exit(process: subprocess.Popen[bytes], deadline: float) -> bool:
    """Whether the unreaped leader exited by `deadline`; it is never reaped here."""
    if process.returncode is not None:
        raise RuntimeError("the group leader was already reaped")
    if sys.platform == "darwin":
        return _darwin_exit(process.pid, deadline)
    while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_POLL_S, remaining))
    return True


def _darwin_exit(pid: int, deadline: float) -> bool:
    if sys.platform != "darwin":
        raise RuntimeError("kqueue exit observation requires macOS")
    kqueue = select.kqueue()
    try:
        watch = select.kevent(
            pid,
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
            fflags=select.KQ_NOTE_EXIT,
        )
        events = kqueue.control([watch], 1, max(0.0, deadline - time.monotonic()))
    finally:
        kqueue.close()
    if not events:
        return False
    event = events[0]
    if event.flags & select.KQ_EV_ERROR:
        # An unreaped child that already exited cannot be watched: ESRCH.
        if event.data != errno.ESRCH:
            raise OSError(event.data, os.strerror(event.data))
        return True
    return bool(event.fflags & select.KQ_NOTE_EXIT)


def group_members(pgid: int) -> list[int]:
    """Every PID in group `pgid`, zombies included."""
    if sys.platform == "darwin":
        return _darwin_group_listing(pgid)
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        # comm may contain spaces or parentheses; fields resume after the last ")".
        fields = stat[stat.rindex(b")") + 2 :].split()
        if int(fields[2]) == pgid:
            members.append(int(entry.name))
    return sorted(members)


@cache
def _proc_listpids() -> Any:
    listpids = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_listpids
    listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    listpids.restype = ctypes.c_int
    return listpids


def _darwin_group_listing(pgid: int) -> list[int]:
    listpids = _proc_listpids()
    ctypes.set_errno(0)
    size = listpids(_PROC_PGRP_ONLY, pgid, None, 0)
    if size <= 0:
        code = ctypes.get_errno()
        raise OSError(code, f"proc_listpids size for group {pgid}: {os.strerror(code)}")
    width = ctypes.sizeof(ctypes.c_int)
    buffer = (ctypes.c_int * (size // width))()
    ctypes.set_errno(0)
    filled = listpids(_PROC_PGRP_ONLY, pgid, buffer, ctypes.sizeof(buffer))
    code = ctypes.get_errno()
    if filled <= 0 and code:
        raise OSError(code, f"proc_listpids for group {pgid}: {os.strerror(code)}")
    return sorted(buffer[: filled // width])


def close_unadmitted(process: subprocess.Popen[bytes], deadline: float) -> int:
    """Kill the group `process` leads, prove it closed, then reap the leader.

    Returns the leader's exit status. Raises `GroupClosureUnresolvedError` at
    `deadline`, retaining the unreaped leader. This is trusted-tool cleanup, not
    a fence: a member that calls setsid() or setpgid() leaves the group.
    """
    group = process.pid
    while True:
        try:
            os.killpg(group, signal.SIGKILL)
        except PermissionError:
            # XNU refuses to signal a group whose members are all zombies.
            if sys.platform != "darwin":
                raise
        if not wait_leader_exit(process, deadline):
            _unresolved(process, f"group {group} leader is still live after SIGKILL")
        members = group_members(group)
        if group not in members:
            _unresolved(process, f"group {group} listing {members} lost its unreaped leader")
        if members == [group]:
            return process.wait()
        if time.monotonic() >= deadline:
            _unresolved(process, f"group {group} still lists members {members}")
        time.sleep(_POLL_S)


def _unresolved(process: subprocess.Popen[bytes], reason: str) -> NoReturn:
    with _UNRESOLVED_LOCK:
        _UNRESOLVED.append(process)
    raise GroupClosureUnresolvedError(reason)
