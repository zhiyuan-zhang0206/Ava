"""Bounded, credential-free process identities for failed detach assertions."""

import ctypes
import os
import sys

import psutil


def detached_to_known_reaper(
    child_pid: int,
    caller_pid: int,
    caller_sid: int,
    ancestor_births: set[tuple[int, float]],
) -> bool:
    """Accept init or a pre-existing ancestor, never the caller or unknown PID."""
    child = psutil.Process(child_pid)
    parent = child.parent()
    if parent is None or parent.pid == caller_pid or os.getsid(child_pid) == caller_sid:
        return False
    return parent.pid == 1 or (parent.pid, parent.create_time()) in ancestor_births


def process_evidence(pid: int) -> dict[str, object]:
    result: dict[str, object] = {"pid": pid}
    try:
        proc = psutil.Process(pid)
        result.update(
            name=proc.name(),
            birth=proc.create_time(),
            ppid=proc.ppid(),
            status=proc.status(),
            pgid=os.getpgid(pid),
            sid=os.getsid(pid),
        )
    except (psutil.NoSuchProcess, ProcessLookupError):
        result["observation"] = "gone"
    except (psutil.AccessDenied, PermissionError):
        result["observation"] = "unreadable"
    return result


def detach_evidence(child_pid: int) -> dict[str, object]:
    """Report ancestry, not argv/env (which can contain credentials)."""
    current = psutil.Process()
    ancestors = current.parents()
    subreaper = ctypes.c_int(-1)
    if sys.platform == "linux":
        # PR_GET_CHILD_SUBREAPER reads THIS process only; do not pretend it
        # establishes a foreign parent's prctl flag.
        result = ctypes.CDLL(None, use_errno=True).prctl(37, ctypes.byref(subreaper), 0, 0, 0)
        if result != 0:
            subreaper.value = -1
    return {
        "caller": process_evidence(current.pid),
        "child": process_evidence(child_pid),
        "caller_subreaper": subreaper.value,
        "caller_ancestors": [process_evidence(parent.pid) for parent in ancestors[:16]],
    }


def no_new_direct_children(
    spawner: psutil.Process, before: set[psutil.Process]
) -> tuple[bool, object]:
    """(ok, state) when `spawner` has no direct child beyond the pre-spawn snapshot."""
    try:
        lingering = set(spawner.children()) - before
    except psutil.NoSuchProcess:
        return True, "spawner gone"
    return not lingering, [c.pid for c in lingering]


def no_zombie_children(spawner: psutil.Process, before: set[psutil.Process]) -> tuple[bool, object]:
    """(ok, state) when none of `spawner`'s recursive children born after the
    pre-spawn snapshot is a zombie.

    The spawner is the pytest worker, which other tests share: a zombie some
    earlier test left behind is not this test's, so the snapshot excludes it.
    Children that exit between the snapshot and the status read are skipped —
    a reaped child is exactly what the no-zombie assertion wants.
    """
    try:
        children = set(spawner.children(recursive=True)) - before
    except psutil.NoSuchProcess:
        return True, "spawner gone"
    zombies: list[int] = []
    for child in children:
        try:
            if child.status() == psutil.STATUS_ZOMBIE:
                zombies.append(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return not zombies, zombies
