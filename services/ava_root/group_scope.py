"""What root reads of a POSIX unit's process group, and the refusals naming it.

Each helper reads, captures or names; none signals. The supervisor decides
when a group number still proves the unit's scope (see `supervisor`).
"""

from __future__ import annotations

import contextlib

import psutil

from services.ava_root.custody import ServiceCustody
from shared.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from shared.process_group_closure import group_empty, group_members


def group_closed(pgid: int) -> bool:
    """Whether a just-reaped leader's group is empty; an unreadable group is not."""
    try:
        return group_empty(pgid)
    except OSError:
        return False


def recorded_living(tracked: set[OwnedProcess]) -> set[OwnedProcess]:
    """Retain the verified descendants of live recorded births; return every live one."""
    for item in {item for item in tracked if item.live()}:
        retain_processes(tracked, capture_tree(item))
    return {item for item in tracked if item.live()}


def ownership_retained(unit_id: str, living: set[OwnedProcess], pgid: int) -> RuntimeError:
    survivors = sorted(item.pid for item in living) or group_members(pgid)
    return RuntimeError(f"unit {unit_id} did not stop; ownership retained (pids {survivors})")


def unproven_group(unit_id: str, pgid: int, custody: ServiceCustody) -> RuntimeError:
    try:
        listed = f"pids {group_members(pgid)}"
    except OSError as exc:
        listed = f"members root cannot list ({exc})"
    return RuntimeError(
        f"unit {unit_id}: every recorded birth has exited, but process group {pgid} still "
        f"holds {listed}; its leader was reaped earlier, so that group may now be another "
        f"program's. Custody retained at {custody.path}. Stop any of those processes that "
        "belong to this unit and retry the stop; if the rest are another program's, move "
        "that record aside once no process of this unit remains"
    )


def group_births(pgid: int) -> set[OwnedProcess]:
    """Native births the kernel files under `pgid` now; lineage is the caller's proof."""
    members: set[OwnedProcess] = set()
    for pid in group_members(pgid):
        with contextlib.suppress(psutil.NoSuchProcess):
            members.add(OwnedProcess.capture(psutil.Process(pid)))
    return members


def capture_group(tracked: set[OwnedProcess], pgid: int) -> set[OwnedProcess]:
    """Retain the named members of an occupied unit group; return the live ones."""
    members = group_births(pgid)
    retain_processes(tracked, members)
    return {item for item in members if item.live()}
