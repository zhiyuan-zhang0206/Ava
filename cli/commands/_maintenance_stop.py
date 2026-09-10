"""Stop a drained unit's recorded services without escalating to force.

The caller owns the maintenance journal and admission fence. These functions
prove only local recorded process identities; they do not prove remote drain or
stop OS-managed extras. Persistent terminals require a separate work boundary.
"""

from __future__ import annotations

import math
import os
import re
import signal
import time
from collections.abc import Callable

import psutil

from cli.commands._maintenance_stop_report import (
    StopIncompleteError,
    live_identities,
    occupied_groups,
    service_inventory,
)
from shared.paths import run_dir
from shared.proc_tree import OwnedProcess, capture_tree
from shared.pty_sessions._paths import host_identity, host_starttime
from shared.session_backend import (
    SessionBackend,
    WinprocSessionBackend,
    get_backend,
    get_shell_backend,
)
from shared.session_record import SessionRecord, pid_starttime_ticks

_TERMINAL_NAME = re.compile(r"ava-(?:agent-\d+-shell-\d+(?:-|$)|schedule-\d+(?:-|$))")


def deadline_after(timeout: float) -> float:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("maintenance stop timeout must be finite and positive")
    return time.monotonic() + timeout


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("maintenance kept its hold; stop deadline expired")
    return value


def wait_for_exit(
    tracked: set[OwnedProcess],
    deadline: float,
    *,
    groups: tuple[int, ...] = (),
    escalate: Callable[[set[OwnedProcess]], None] | None = None,
) -> None:
    """Wait for `tracked` identities and `groups` to empty, within `deadline`.

    `escalate`, when given, is called once per iteration with the current
    living identities, AFTER the descendant re-capture: it may deliver further
    graceful signals to confirmed-owned identities (issue #2123 — a leader that
    exits without closing its children must not stall the stop until the
    deadline; the caller's escalator owns the ownership validation). Never
    escalates to SIGKILL and never certifies success while anything is alive.
    """
    while True:
        living = {identity for identity in tracked if identity.live()}
        occupied = occupied_groups(groups)
        if not living and not occupied:
            return
        for identity in living:
            tracked.update(capture_tree(identity))
        if escalate is not None:
            escalate(living)
        try:
            budget = remaining(deadline)
        except TimeoutError:
            raise TimeoutError(
                f"maintenance kept its hold; processes did not exit: "
                f"{sorted(identity.pid for identity in living)}; occupied process groups: {occupied}"
            ) from None
        time.sleep(min(0.05, budget))


def _terminate_owned(identity: OwnedProcess) -> bool:
    """TERM one captured identity, re-validated at delivery (never SIGKILL).

    The same delivery discipline as ``posixproc.graceful_signal``: the birth
    identity (starttime ticks where available, create_time elsewhere) is
    re-checked against the live pid immediately before the signal, so a PID
    recycled since capture can never receive a signal meant for its previous
    occupant. Returns False when the identity is already gone or no longer
    verifiable — the caller reports, it does not retry blindly.
    """
    if not identity.live():
        return False
    try:
        proc = psutil.Process(identity.pid)
    except psutil.NoSuchProcess:
        return False
    if identity.starttime is not None:
        if pid_starttime_ticks(identity.pid) != identity.starttime:
            return False
    elif proc.create_time() != identity.birth:
        return False
    try:
        proc.send_signal(signal.SIGTERM)
    except (psutil.NoSuchProcess, ProcessLookupError):
        return False
    return True


def _group_members(pgid: int) -> set[OwnedProcess]:
    """Birth-captured live members of a recorded session group.

    Used only for a leader already dead at stop entry (the reap gate keeps the
    record precisely while the group is occupied). An unreadable member cannot
    certify emptiness, so it refuses instead of guessing.
    """
    members: set[OwnedProcess] = set()
    for process in psutil.process_iter():
        try:
            if os.getpgid(process.pid) != pgid:
                continue
            if process.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                continue
            members.add(OwnedProcess.capture(process))
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except psutil.AccessDenied:
            raise RuntimeError(f"cannot inspect member of process group {pgid}") from None
    return members


def _capture_groups(records: dict[str, SessionRecord]) -> tuple[int, ...]:
    if os.name != "posix":
        return ()
    from shared.posixproc import _pgid_of

    captured: set[int | None] = set()
    for record in records.values():
        identity = OwnedProcess(record.pid, record.create_time, record.starttime)
        if identity.live():
            captured.add(_pgid_of(psutil.Process(record.pid)))
            if not identity.live():
                raise RuntimeError("service identity changed during process-group capture")
        else:
            # Leader already dead at stop entry: the recorded spawn-time pgid
            # is the remaining ownership proof (the listing only keeps such a
            # record while the group is occupied).
            captured.add(record.pgid)
    if None in captured or os.getpgrp() in captured:
        raise RuntimeError("cannot verify an isolated service process group")
    return tuple(group for group in captured if group is not None)


def require_no_terminals() -> None:
    # A PTY host can remain alive after its shell exits. The ordinary listing
    # intentionally omits that retained record, so inspect both recorded births.
    terminals: list[str] = []
    for path in (run_dir() / "pty").glob("*.json"):
        record = SessionRecord.read(path)
        if record is None:
            raise RuntimeError(f"cannot verify terminal record: {path.stem}")
        identities = [OwnedProcess(record.pid, record.create_time, record.starttime)]
        host = host_identity(path)
        if host is not None:
            identities.append(OwnedProcess(host[0], host[1], host_starttime(path)))
        if any(identity.live() for identity in identities):
            terminals.append(path.stem)
    backend = get_shell_backend()
    listed = backend.list_sessions()
    if isinstance(backend, WinprocSessionBackend):
        # Windows uses one record namespace for services and interactive shells.
        # These are the SDK and ScheduleManager's existing terminal name shapes.
        listed = [name for name in listed if _TERMINAL_NAME.match(name)]
    terminals.extend(listed)
    if terminals:
        raise RuntimeError(
            "persistent terminals/schedules require their own completed-work boundary; "
            f"maintenance will not kill or replay them: {sorted(set(terminals))}"
        )


def service_names(backend: SessionBackend, *, keep_terminals: bool = False) -> list[str]:
    """Select services from Windows' shared service/terminal record namespace."""
    names = backend.list_sessions()
    if keep_terminals and isinstance(backend, WinprocSessionBackend):
        names = [name for name in names if not _TERMINAL_NAME.match(name)]
    return sorted(names)


def _validate_service_records(
    backend: SessionBackend, *, keep_terminals: bool, selected: frozenset[str] | None = None
) -> None:
    # List APIs may discard malformed or stale records; do not let that erase
    # an identity uncertainty before strict preflight has examined it.
    for path in (run_dir() / "sessions").glob("*.json"):
        if selected is not None and path.stem not in selected:
            continue
        if (
            keep_terminals
            and selected is None
            and isinstance(backend, WinprocSessionBackend)
            and _TERMINAL_NAME.match(path.stem)
        ):
            continue
        record = SessionRecord.read(path)
        if record is None:
            raise RuntimeError(f"cannot verify service record: {path.stem}")
        identity = OwnedProcess(record.pid, record.create_time, record.starttime)
        if not identity.live():
            try:
                current = OwnedProcess.capture(psutil.Process(record.pid))
            except psutil.NoSuchProcess:
                continue
            if current.live():
                raise RuntimeError(f"service identity changed before stop: {path.stem}")


def _capture_services(
    names: list[str], deadline: float
) -> tuple[
    dict[str, SessionRecord],
    dict[str, OwnedProcess],
    dict[str, set[OwnedProcess]],
    set[OwnedProcess],
]:
    """Read every listed record and capture what it owns.

    A live leader contributes its full descendant tree. A leader already dead
    is listed only while its recorded spawn-time process group is still
    occupied (the reap gate) — the retry / post-crash stop path converges the
    surviving group members; a missing group (legacy record) keeps the strict
    refusal.
    """
    records: dict[str, SessionRecord] = {}
    leaders: dict[str, OwnedProcess] = {}
    by_service: dict[str, set[OwnedProcess]] = {}
    tracked: set[OwnedProcess] = set()
    ancestors = {os.getpid(), *(process.pid for process in psutil.Process().parents())}
    for name in names:
        remaining(deadline)
        record = SessionRecord.read(run_dir() / "sessions" / f"{name}.json")
        if record is None:
            raise RuntimeError(f"cannot read exact identity of service {name}")
        if record.pid in ancestors:
            raise RuntimeError("maintenance stop must run outside the unit's service tree")
        identity = OwnedProcess(record.pid, record.create_time, record.starttime)
        if identity.live():
            records[name] = record
            leaders[name] = identity
            tree = capture_tree(identity)
            by_service[name] = tree
            tracked.update(tree)
        else:
            group = record.pgid
            if group is None or group == os.getpgrp():
                raise RuntimeError(f"service identity changed before stop: {name}")
            members = _group_members(group)
            if not members:
                continue  # already stopped; the stale record reaps at listing
            records[name] = record
            leaders[name] = identity
            by_service[name] = members
            tracked.update(members)
    return records, leaders, by_service, tracked


def stop_services(
    timeout: float, *, keep_terminals: bool = False, selected: frozenset[str] | None = None
) -> list[str]:
    """Signal captured service identities; a survivor leaves maintenance held.

    No kill_session fallback is allowed. Windows uses the existing private
    console helper with the remaining budget and expected record identity.

    POSIX convergence contract (issue #2123): the leader receives the graceful
    signal; once the leader is confirmed gone, its captured descendants — a
    frontend chain's surviving Next.js, an agent's leftovers after its finally
    ran — receive TERM too, each re-validated by birth identity at delivery and
    at most once. A leader that is already dead at entry (retry / post-crash)
    is converged through its recorded spawn-time process group, which the
    listing retains exactly while it is occupied. Descendants that refuse TERM
    keep the hold and are reported at the deadline, never SIGKILLed. Original
    POSIX groups remain checked after their leaders exit; captured descendants
    that left those groups are followed by birth identity. Unknown newly
    daemonized sessions require separate ownership proof. Admission fencing and
    separately registered resources remain caller duties.
    keep_terminals is an operator assertion of a separately verified work
    boundary; it preserves terminals without proving they have stopped writing.

    When the deadline expires anyway, the raised error carries every survivor's
    full identity — owning session, leader/descendant role, birth pair,
    cmdline, and the recorded groups still occupied — so an operator can act on
    the exact resource instead of rerunning blind (issue #2162).
    """
    deadline = deadline_after(timeout)
    if not keep_terminals:
        require_no_terminals()
    backend = get_backend()
    _validate_service_records(backend, keep_terminals=keep_terminals, selected=selected)

    def current_names() -> list[str]:
        names = service_names(backend, keep_terminals=keep_terminals and selected is None)
        return names if selected is None else [name for name in names if name in selected]

    names = current_names()
    records, leaders, by_service, tracked = _capture_services(names, deadline)
    # The POSIX backend gives each service an isolated process group. Retain
    # it independently of the leader: a normal shutdown handler can fork and
    # exit before the next descendant snapshot. Never signal the group here;
    # only individually validated members receive TERM (issue #2123).
    groups = _capture_groups(records)
    # Complete every identity/preflight check before delivering the first signal.
    if not keep_terminals:
        require_no_terminals()
    if current_names() != names:
        raise RuntimeError("service roster changed before held stop")
    ordered = sorted(names, key=lambda name: not name.endswith("watchdog"))
    for name in ordered:
        remaining(deadline)
        if not leaders[name].live():
            continue  # leader dead at entry — converged by the escalator below
        if isinstance(backend, WinprocSessionBackend):
            delivered = backend.graceful_signal(
                name, expected=records[name], timeout=remaining(deadline)
            )
        else:
            delivered = backend.graceful_signal(name, expected=records[name])
        remaining(deadline)
        if not delivered:
            record = records[name]
            if OwnedProcess(record.pid, record.create_time, record.starttime).live():
                raise RuntimeError(f"graceful signal refused the captured service: {name}")
    signalled: set[OwnedProcess] = set()

    def escalate(living: set[OwnedProcess]) -> None:
        # TERM a dead leader's surviving descendants, each validated by birth.
        # Only after the leader is confirmed gone: a live leader still runs its
        # own graceful shutdown (an agent's finally closes the children it
        # tracks), so its tree is left alone. Descendants are signalled at most
        # once; a member that refuses TERM keeps the hold and is reported by
        # the timeout, never force-killed (issue #2123).
        for name, tree in by_service.items():
            for identity in list(tree & living):
                tree.update(capture_tree(identity))
            tracked.update(tree)
            if leaders[name].live():
                continue
            for identity in tree:
                if identity.live() and identity not in signalled and _terminate_owned(identity):
                    signalled.add(identity)

    try:
        wait_for_exit(tracked, deadline, groups=groups, escalate=escalate)
    except TimeoutError as exc:
        live_leaders = live_identities(leaders.values())
        surviving = sorted(name for name, identity in leaders.items() if identity in live_leaders)
        surviving_tracked = [identity.pid for identity in live_identities(tracked)]
        inventory = service_inventory(
            records=records, leaders=leaders, by_service=by_service, tracked=tracked, groups=groups
        )
        raise StopIncompleteError(
            f"service stop incomplete — {exc} surviving services: {surviving or 'unknown'}; "
            f"surviving tracked descendants: {surviving_tracked}\n"
            f"{inventory.render(stage='services')}",
            stage="services",
            survivors=inventory.payload(),
        ) from exc
    remaining(deadline)
    if not keep_terminals:
        require_no_terminals()
    if current_names():
        raise RuntimeError("services appeared during held stop")
    return names


def stop_data_plane(timeout: float, *, save: bool = True) -> list[str]:
    """Stop this home's native data plane; never stop a remote-managed plane."""
    from cli.commands._maintenance_data_plane import stop

    return stop(timeout, save=save)
