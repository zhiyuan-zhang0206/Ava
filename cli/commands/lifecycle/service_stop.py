"""Stop a drained unit's recorded services and persistent terminals.

The caller owns the maintenance journal and admission fence. These functions
prove only local recorded process identities; they do not prove remote drain or
stop OS-managed extras. Service stops never escalate to force.

Persistent terminals have one closure (`base.sessions.pty.closure`), run by the
pty-sessions service that holds every shell: HUP the shells and TERM the rest of
each captured POSIX session, wait a bounded grace, SIGKILL what is left, each
session whole. `close_terminals` asks the service for it at `ava stop`
(decisions/2026-09-28-stop-escalates-to-sigkill.md) and gives every busy session
whose shell it verified gone its owner's notice, naming what of it outlived the
SIGKILL. A service that is not running is closed from its ledger instead
(`services.pty_sessions.ledger.sweep`), and its owners are told it crashed, not
that this stop closed their sessions. KILL reaches only identities the service
captured from its sessions' shells.
"""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from base import telemetry
from base.cluster import postgres as owned_postgres
from base.db import Database
from base.events.live.bus import EventBus
from base.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from base.sessions.pty import client, closure
from base.sessions.pty.paths import ledger_path
from cli.commands.lifecycle._maintenance_stop_report import (
    StopIncompleteError,
    SurvivorInventory,
    capture_survivor,
    live_identities,
    occupied_groups,
)
from ops import pty_close_notices
from services.pty_sessions import ledger

# How long a normal stop's terminal closure waits between its HUP/TERM and the
# SIGKILL of whatever is left (decisions/2026-09-28-stop-escalates-to-sigkill.md):
# a job that handles TERM gets this long to clean up. The stop's own deadline
# caps it as well.
_TERMINAL_STOP_GRACE_S = 10.0

# The SIGKILL leg's own bound at a normal stop: each wait inside a session kill,
# the wait for the killed sessions' hosts to end, and the least the closure
# evidence waits (it also gets the rest of the stop deadline).
# Every wait ends as soon as its processes are gone. The leg runs even when the
# grace spent the rest of the stop deadline — a stop that reached its terminal
# phase closes its terminals — so a stop can overrun its deadline by this
# bounded leg.
_TERMINAL_KILL_WAIT_S = 3.0

# The data-plane stop's escalation legs (`cli/commands/data_plane/maintenance_stop.py`)
# use the same two bounds under their own names: how long Postgres' immediate shutdown
# is given to finish, and how long the SIGKILL of what outlives it is waited for.
PROCESS_CLEANUP_WAIT_S = _TERMINAL_STOP_GRACE_S
PROCESS_KILL_WAIT_S = _TERMINAL_KILL_WAIT_S


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
            retain_processes(tracked, capture_tree(identity))
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


def require_no_terminals() -> None:
    """Refuse maintenance while any terminal is present (`live_terminals`)."""
    terminals = live_terminals()
    if terminals:
        raise RuntimeError(
            "persistent terminals/schedules require their own completed-work boundary; "
            f"maintenance will not kill or replay them: {terminals}"
        )


def live_terminals() -> list[str]:
    """Every live terminal: the service's sessions, or a dead service's leftovers.

    With the service listening its listing is the truth. Without it, what its ledger
    names and still runs is a crash's leftover: a process that outlived the hangup.
    """
    try:
        return [row["name"] for row in client.request("list")["sessions"]]
    except client.ServiceDownError:
        return ledger.leftovers(ledger_path())


def _close_via_service(grace_s: float, kill_s: float) -> closure.Outcome:
    """Run the one closure where the shells are: in the service (raises when it is down)."""
    return client.close_all(grace_s=grace_s, kill_s=kill_s)


def _close_terminals_now(grace_s: float, kill_s: float) -> tuple[closure.Outcome, str]:
    """Close every terminal in the service, else from its ledger; with the reason owners are told.

    A service that is down died uncleanly before this stop (the stop never
    leaves one behind): what its ledger still names was lost to that, not to
    this stop, and its notices say so (`pty_close_notices.CRASH_REASON`).
    """
    try:
        return _close_via_service(grace_s, kill_s), pty_close_notices.STOP_REASON
    except client.ServiceDownError:
        return ledger.sweep(ledger_path()), pty_close_notices.CRASH_REASON


def _terminals_incomplete(outcome: closure.Outcome, stage: str) -> StopIncompleteError:
    """The report for processes that outlived their SIGKILL (issue #2162's inventory).

    Each survivor names its owning session and its identity so the operator
    can find and judge the exact process — typically another user's (a root
    `sudo`), which this closure may not signal.
    """
    live = {s.process: s for s in outcome.survivors if live_identities([s.process])}
    report = [
        capture_survivor(identity, service=found.session, role=found.role)
        for identity, found in sorted(live.items(), key=lambda item: item[0].pid)
    ]
    surviving = sorted({found.session for found in live.values()})
    inventory = SurvivorInventory(survivors=report, groups=[])
    return StopIncompleteError(
        f"terminal closure incomplete — processes outlived their SIGKILL: "
        f"{[identity.pid for identity in live]} from sessions: {surviving}\n"
        f"{inventory.render(stage=stage, killed=True)}",
        stage=stage,
        survivors=[survivor.payload() for survivor in report],
    )


@dataclass(frozen=True)
class _Notice:
    """What a closure's owner notices name (issue #2044): its operation, hold and reason."""

    operation: str
    acquired_at: datetime
    reason: str


def _await_no_terminals(until: float, stage: str) -> None:
    """The closure evidence: no terminal is live; raise at `until`.

    The service drops a session from its table the moment its teardown is claimed,
    so this only waits out one still tearing down; a terminal born during the
    closure fails it.
    """
    while True:
        left = live_terminals()
        if not left:
            return
        if time.monotonic() >= until:
            raise StopIncompleteError(
                f"terminal closure incomplete — terminals still present after the closure "
                f"ended every session it captured: {left}",
                stage=stage,
            )
        time.sleep(0.05)


def _record_close_notices(
    closed: tuple[closure.ClosedSession, ...], notice: _Notice, *, direct_db: bool
) -> None:
    """Write one closure notice per closed busy session to the database (issue #2044).

    Each entry names the session's shell and the processes of it that outlived
    the SIGKILL. The write is one short connection and one transaction, made
    here while the data plane is still up and closed before this returns
    (`pty_close_notices`): the batch commits or rolls back as one, so a failed
    write leaves nothing behind to skip a later re-send. An idle session or one
    that is not an agent shell yields no notice, and no notice means no
    connection. A batch that cannot be written is loud on stderr, with the text
    its owner would have read, but never fails the closure — retrying the whole
    stop would not restore the resources it closes.
    """
    notices = pty_close_notices.notices_for(
        closed, reason=notice.reason, operation=notice.operation, acquired_at=notice.acquired_at
    )
    for unwritten, exc in pty_close_notices.write_notices(
        Database.from_settings(), EventBus.from_settings(), notices, direct=direct_db
    ):
        # The side-channel notice must never fail a closure; stay loud so the
        # gap is visible either way.
        print(
            f"closure notice for session {unwritten.name!r} (agent {unwritten.agent_id}) "
            f"could not be written: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def close_terminals(
    deadline: float, operation: str, acquired_at: datetime, *, direct_db: bool
) -> None:
    """Close this unit's terminals at `ava stop`: HUP/TERM, a bounded grace, then SIGKILL.

    The grace is `_TERMINAL_STOP_GRACE_S`, capped by the stop's `deadline`;
    the SIGKILL leg is bounded by `_TERMINAL_KILL_WAIT_S` and runs even when
    the grace spent the rest of the deadline — a stop that reached its
    terminal phase closes its terminals
    (decisions/2026-09-28-stop-escalates-to-sigkill.md). A process that
    outlives its SIGKILL fails the stop, which keeps its maintenance hold. A
    terminal still tearing down after that gets the rest of the deadline, and
    at least `_TERMINAL_KILL_WAIT_S`, to leave the service's table.

    Busy sessions whose shell is verified gone — a job the SIGKILL cut short
    included — get a closure notice for their owner agent (issue #2044),
    written to the database over one short connection here, before the data
    plane stops (`direct_db`: this unit's own Postgres, bypassing its pooler,
    rather than the gateway's database a runner-only unit dials). That holds
    when a process outlived the SIGKILL too (the notice names it) and when
    another session keeps the stop incomplete.
    """
    grace_s = max(0.0, min(deadline - time.monotonic(), _TERMINAL_STOP_GRACE_S))
    outcome, reason = _close_terminals_now(grace_s, _TERMINAL_KILL_WAIT_S)
    _record_close_notices(
        outcome.closed,
        _Notice(operation, acquired_at, reason),
        direct_db=direct_db,
    )
    if any(live_identities([s.process]) for s in outcome.survivors):
        raise _terminals_incomplete(outcome, "terminals")
    _await_no_terminals(max(deadline, time.monotonic() + _TERMINAL_KILL_WAIT_S), "terminals")


def force_close_terminals() -> None:
    """Close this unit's terminals at `ava stop --force`: no grace, no notices.

    Force skips the owner notices and the drain guarantees by definition; a process
    that outlives the SIGKILL still fails it, naming the session.
    """
    outcome, _reason = _close_terminals_now(0.0, _TERMINAL_KILL_WAIT_S)
    if any(live_identities([s.process]) for s in outcome.survivors):
        raise RuntimeError(
            f"force stop did not close terminals: {sorted({s.session for s in outcome.survivors})}"
        )


def report_postgres_stop_escalation(
    escalation: owned_postgres.Escalation, notes: list[str] | None = None
) -> None:
    """Report a Postgres shutdown that had to be ended by an immediate one.

    The owner (`base.cluster.postgres`) logs the escalation; this adds the
    operator line and the `postgres_stop_escalated` event. `notes`, when the
    caller owns a stop journal, collects the line for it
    (`_temporary_stop._finish_stop`); a leg that owns no journal passes
    nothing and still gets stderr and the event
    (decisions/2026-10-02-pg-stop-escalates-to-immediate.md).
    """
    killed = ", ".join(str(pid) for pid in escalation.killed) or "none"
    note = (
        f"postgres {escalation.detail}; ended by an immediate shutdown "
        f"(crash recovery at the next start; unarchived WAL stays in pg_wal), "
        f"killed leftover processes: {killed}"
    )
    print(f"  ! {note}", file=sys.stderr, flush=True)
    telemetry.emit(
        "telemetry",
        "postgres_stop_escalated",
        level="error",
        source="stop",
        attributes={"detail": escalation.detail, "killed": list(escalation.killed)},
    )
    if notes is not None:
        notes.append(note)


def stop_data_plane(
    timeout: float,
    *,
    save: bool = True,
    notes: list[str] | None = None,
    clients: list[str] | None = None,
) -> list[str]:
    """Stop this home's native data plane; never stop a remote-managed plane.

    `notes` collects what the stop report must say (a Postgres shutdown that had to be
    escalated); `clients` collects the pooler's still-connected clients, reported and
    never acted on.
    """
    from cli.commands.data_plane.maintenance_stop import stop

    return stop(timeout, save=save, notes=notes, clients=clients)
