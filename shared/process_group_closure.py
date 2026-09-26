"""The one closure core for a process group whose leader this process launched.

A bare interpreter can import this module (no psutil, no pydantic), so the
checkout-retiring runtime proof and the release-store contract use it; the exec
domain, PITR operation custody and ava-root unit stop build on it too.

The direct child launched as its own group's leader (`process_group=0` or a new
session) stays UNREAPED until closure is proven: its zombie keeps the group
number reserved, so every group signal reaches only this launch. Its exit is
observed without reaping (kqueue NOTE_EXIT on macOS, `waitid(WNOWAIT)` on
Linux). Closure is a kernel group listing that names nothing but the exited
leader, read after a group-wide SIGKILL: XNU's `proc_listpids(PROC_PGRP_ONLY)`
snapshot on macOS, a `/proc` scan on Linux, where a fork racing the group
SIGKILL either fails or hands the pending signal to the new child. XNU instead
lets a member inside fork() when the signal lands complete it, and that child
never receives the signal. So any other listed member, live or zombie, forces
another round. `confirm_closure` proves closure and leaves the leader to its
caller's custody; `close_unadmitted` also reaps it. An unresolved closure
leaves the leader unreaped.
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
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

_PROC_PGRP_ONLY = 2  # <sys/proc_info.h>: list PIDs by process-group id.
_POLL_S = 0.05

# Leaders whose closure is unresolved. Holding them keeps Popen's finalizer from
# queueing them for a later reap, which would release their group numbers.
_UNRESOLVED: list[subprocess.Popen[bytes]] = []
_UNRESOLVED_LOCK = threading.Lock()


class GroupClosureUnresolvedError(TimeoutError):
    """Closure was not proven by its deadline; the leader stays unreaped."""


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
    """Every PID in group `pgid`, zombies included.

    macOS reads one kernel snapshot. Linux scans `/proc`, so a member can exit
    or appear during the scan: only after a group SIGKILL does a listing of the
    leader alone prove closure there (`confirm_closure`).
    """
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


def group_empty(pgid: int) -> bool:
    """Whether no process, live or zombie, remains in group `pgid`.

    Only meaningful once the group's leader was reaped: the number stays
    reserved while any member exists, so no other group can take it. macOS
    reads the kernel group listing. Linux sends the group a null signal, which
    walks the group under the tasklist lock that fork holds to add a child to
    it, so a member mid-fork keeps the answer false. Neither is an
    enumerate-then-read scan.
    """
    if sys.platform == "darwin":
        return not _darwin_group_listing(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    return False


@cache
def _proc_listpids() -> Any:
    listpids = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True).proc_listpids
    listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    listpids.restype = ctypes.c_int
    return listpids


def _darwin_group_listing(pgid: int) -> list[int]:
    """Every PID XNU files under `pgid`, zombies included, as one snapshot.

    `proc_listpids(PROC_PGRP_ONLY)` walks allproc then zombproc under the
    proc-list lock that fork, exit and reap take to change those lists, unlike
    an enumerate-then-read scan. A result filling the buffer may be truncated;
    it already names more than the leader, which never confirms closure.
    """
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


def wait_group_finished(process: subprocess.Popen[bytes], deadline: float) -> bool:
    """Natural completion by `deadline`: the leader exited and its group lists nothing else.

    Never signals or reaps. It is not a closure proof (on Linux an unsignalled
    member can fork past the scan), so the caller closes the group afterwards.
    """
    if not wait_leader_exit(process, deadline):
        return False
    while group_members(process.pid) != [process.pid]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_POLL_S, remaining))
    return True


def confirm_closure(
    process: subprocess.Popen[bytes],
    deadline: float,
    signal_group: Callable[[], None] | None = None,
) -> None:
    """Kill the group `process` leads and prove it closed; the leader stays unreaped.

    Each round sends one group-wide SIGKILL, waits for the leader's exit and
    lists the group; it returns once the listing names only the leader. The
    default signal refuses a reaped leader and accepts XNU's EPERM for an
    all-zombie group, since the listing decides. A caller with its own signal
    authority passes `signal_group`; whatever it raises propagates. Raises
    `GroupClosureUnresolvedError` at `deadline`. This is trusted-tool cleanup,
    not a fence: a member that calls setsid() or setpgid() leaves the group.
    """
    group = process.pid
    while True:
        if signal_group is None:
            _kill_group(process)
        else:
            signal_group()
        if not wait_leader_exit(process, deadline):
            raise GroupClosureUnresolvedError(
                f"group {group} leader is still live after its group signal"
            )
        members = group_members(group)
        if group not in members:
            raise RuntimeError(f"group {group} listing {members} lost its unreaped leader")
        others = [pid for pid in members if pid != group]
        if not others:
            return
        if time.monotonic() >= deadline:
            raise GroupClosureUnresolvedError(
                f"group {group} still lists members {others} besides its leader"
            )
        time.sleep(_POLL_S)


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    if process.returncode is not None:
        raise RuntimeError("the group leader was already reaped")
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except PermissionError:
        # XNU refuses to signal a group whose members are all zombies.
        if sys.platform != "darwin":
            raise


def close_unadmitted(process: subprocess.Popen[bytes], deadline: float) -> int:
    """Close the group of a launch no owner admitted, then reap its leader.

    Returns the leader's exit status. Any failure, including
    `GroupClosureUnresolvedError` at `deadline`, keeps the unreaped leader
    referenced for the life of this process.
    """
    try:
        confirm_closure(process, deadline)
    except BaseException:
        with _UNRESOLVED_LOCK:
            _UNRESOLVED.append(process)
        raise
    return process.wait()
