"""What root reads of a POSIX unit's process group, and the refusals naming it.

Each helper reads, captures, records or names; none signals. The supervisor
decides when a group number still proves the unit's scope (see `supervisor`).
"""

from __future__ import annotations

import logging
import os

import psutil

from base.native_process.group_closure import group_empty, group_members
from base.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from services.supervision.ava_root.custody import ServiceCustody

_log = logging.getLogger(__name__)


def group_closed(pgid: int) -> bool:
    """Whether a just-reaped leader's group is empty; an unreadable group is not."""
    try:
        return group_empty(pgid)
    except OSError:
        return False


def group_over(pgid: int, *, empty_at_exit: bool) -> bool:
    """Whether the group a reaped unit leader led has ended; nothing is signalled.

    It has when it was empty at the read after the reap, its number is now
    held as a PID by another process (a PID is never reused while it is still
    the process-group ID of a live group, POSIX), the group carrying that
    number now lies in another session (`_foreign_session`), or it is empty
    now. Emptiness is read last: a group that empties during the slower reads
    before it still counts as over, and a refusal follows the reading it reports.
    """
    return empty_at_exit or psutil.pid_exists(pgid) or _foreign_session(pgid) or group_closed(pgid)


def _foreign_session(pgid: int) -> bool:
    """Whether group `pgid` now lies outside root's session, so it is not the unit's.

    Root births each unit leader into a group of its own inside root's session
    (setpgid, never setsid) and never leaves that session: every launch makes
    root its leader, and a session leader cannot call setsid(). POSIX keeps all
    members of a group in one session, and a process joins only a group of its
    own session, so a group in another session holds no process of root's
    session: the unit's group ended before that group formed. A unit process
    that called setsid() escaped by construction and was never covered.

    One member decides. Its session is read before its group, and its birth
    brackets both reads: a member that calls setsid() in between reads its own
    group, never `pgid`; a listed PID now naming another birth, or outside the
    group, decides nothing. A member in root's session, or no readable member,
    returns False, so the stop keeps custody.
    """
    own = os.getsid(0)
    try:
        members = group_members(pgid)
    except OSError:
        return False
    for pid in members:
        try:
            member = OwnedProcess.capture(psutil.Process(pid))
            session = os.getsid(pid)
            group = os.getpgid(pid)
            if group != pgid or not member.live():
                continue
        except (OSError, RuntimeError, psutil.Error):
            continue
        if session == own:
            return False
        _log.info("process group %s now lies in session %s, not root's %s", pgid, session, own)
        return True
    return False


def recorded_living(tracked: set[OwnedProcess]) -> set[OwnedProcess]:
    """Retain the verified descendants of live recorded births; return every live one."""
    for item in {item for item in tracked if item.live()}:
        retain_processes(tracked, capture_tree(item))
    return {item for item in tracked if item.live()}


def ownership_retained(
    unit_id: str, living: set[OwnedProcess], pgid: int, window_s: float
) -> RuntimeError:
    survivors = sorted(item.pid for item in living) or group_members(pgid)
    return RuntimeError(
        f"unit {unit_id} did not stop within its {window_s:g}s window; "
        f"ownership retained (pids {survivors})"
    )


def unproven_group(unit_id: str, pgid: int, custody: ServiceCustody) -> RuntimeError:
    try:
        listed = f"pids {group_members(pgid)}"
    except OSError as exc:
        listed = f"members root cannot list ({exc})"
    return RuntimeError(
        f"unit {unit_id}: every recorded birth has exited, but process group {pgid} still "
        f"holds {listed} and root cannot place that group outside its own session, so they "
        f"may be unrecorded processes of this unit. Custody retained at {custody.path}. Stop "
        "any of those processes that belong to this unit and retry the stop; if the rest are "
        "another program's, move that record aside once no process of this unit remains and "
        "retry the stop, which then drops this unit's generation without a signal"
    )


def group_births(pgid: int) -> set[OwnedProcess]:
    """Native births the kernel files under `pgid` now; lineage is the caller's proof.

    A member whose birth cannot be read (access denied, or no Linux start ticks)
    is logged and left out alone, so it is never signalled; the rest still count.
    """
    members: set[OwnedProcess] = set()
    for pid in group_members(pgid):
        try:
            members.add(OwnedProcess.capture(psutil.Process(pid)))
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, RuntimeError) as exc:
            _log.warning("process group %s: member %s not captured: %s", pgid, pid, exc)
    return members


def record_survivors(
    unit_id: str, pgid: int, tracked: set[OwnedProcess], custody: ServiceCustody | None
) -> None:
    """Retain the births in a just-reaped leader's group while its number is reserved.

    Never raises, so the watch still publishes the exit. A member whose birth
    cannot be read is left out alone (`group_births`) and never signalled later;
    it keeps the group occupied, and the stop then refuses instead of releasing
    custody.
    """
    try:
        retain_processes(tracked, group_births(pgid))
        if custody is not None:
            custody.retain(tracked, pgid)
    except (OSError, RuntimeError, psutil.Error) as exc:
        _log.error("unit %s: group %s survivors not fully recorded at reap: %s", unit_id, pgid, exc)


def capture_group(tracked: set[OwnedProcess], pgid: int) -> set[OwnedProcess]:
    """Retain the named members of an occupied unit group; return the live ones."""
    members = group_births(pgid)
    retain_processes(tracked, members)
    return {item for item in members if item.live()}
