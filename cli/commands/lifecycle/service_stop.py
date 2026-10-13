"""Stop a drained unit's recorded services and persistent terminals.

The caller owns the maintenance journal and admission fence. These functions
prove only local recorded process identities; they do not prove remote drain or
stop OS-managed extras. Service stops never escalate to force.

Persistent terminals use a bounded best-effort closure: close the known shell
and terminal, signal known shell/foreground groups, and report known leftovers.
A surviving shell fails the stop; a known job leftover is diagnostic. Closure
never proves that every descendant or detached process disappeared. A stopped
service's ledger supplies only recorded birth identities, never a global scan.
See docs/decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentity
from base.events.live.bus import EventBus
from base.sessions.pty import client, closure
from base.sessions.pty.paths import ledger_path
from base.telemetry import EventPipeline
from cli.commands.data_plane.maintenance_stop_report import (
    StopIncompleteError,
    SurvivorInventory,
    capture_survivor,
    live_identities,
)
from cli.commands.data_plane.stop_waits import (
    PROCESS_CLEANUP_WAIT_S,
    PROCESS_KILL_WAIT_S,
)
from ops import pty_close_notices
from services.agent_runner.pty_sessions import ledger

# The unit stop's terminal closure uses the shared stop bounds (`data_plane/stop_waits.py`).
_TERMINAL_STOP_GRACE_S = PROCESS_CLEANUP_WAIT_S
_TERMINAL_KILL_WAIT_S = PROCESS_KILL_WAIT_S


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
    """Report known shells that remain alive after bounded closure."""
    live = {
        s.process: s
        for s in outcome.survivors
        if s.role == "terminal" and live_identities([s.process])
    }
    report = [
        capture_survivor(identity, service=found.session, role=found.role)
        for identity, found in sorted(live.items(), key=lambda item: item[0].pid)
    ]
    surviving = sorted({found.session for found in live.values()})
    inventory = SurvivorInventory(survivors=report, groups=[])
    return StopIncompleteError(
        f"terminal closure incomplete — known shells remain alive: "
        f"{[identity.pid for identity in live]} from sessions: {surviving}\n"
        f"{inventory.render(stage=stage, killed=True)}",
        stage=stage,
        survivors=[survivor.payload() for survivor in report],
    )


def _report_terminal_job_leftovers(outcome: closure.Outcome) -> None:
    """Report known living jobs without turning best-effort closure into a stop gate."""
    for survivor in outcome.survivors:
        if survivor.role != "terminal" and live_identities([survivor.process]):
            print(
                f"terminal closure {survivor.session!r}: known job still running: "
                f"pid={survivor.process.pid}; inspect the process before taking action",
                file=sys.stderr,
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
    closed: tuple[closure.ClosedSession, ...],
    notice: _Notice,
    *,
    direct_db: bool,
    database_factory: DatabaseFactory,
    machine_name: str,
) -> None:
    """Write one closure notice per closed busy session to the database (issue #2044).

    Each entry names the session's shell and known observed job leftovers. The write is one short connection and one transaction, made
    here while the data plane is still up and closed before this returns
    (`pty_close_notices`): the batch commits or rolls back as one, so a failed
    write leaves nothing behind to skip a later re-send. An idle session or one
    that is not an agent shell yields no notice, and no notice means no
    connection. A batch that cannot be written is loud on stderr, with the text
    its owner would have read, but never fails the closure — retrying the whole
    stop would not restore the resources it closes.
    """
    notices = pty_close_notices.notices_for(
        closed,
        reason=notice.reason,
        operation=notice.operation,
        acquired_at=notice.acquired_at,
        local_machine=machine_name,
    )
    for unwritten, exc in pty_close_notices.write_notices(
        database_factory(), EventBus.from_settings(), notices, direct=direct_db
    ):
        # The side-channel notice must never fail a closure; stay loud so the
        # gap is visible either way.
        print(
            f"closure notice for session {unwritten.name!r} (agent {unwritten.agent_id}) "
            f"could not be written: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )


def close_terminals(
    deadline: float,
    operation: str,
    acquired_at: datetime,
    *,
    direct_db: bool,
    database_factory: DatabaseFactory,
    local_machine: MachineIdentity,
) -> None:
    """Close this unit's terminals at `ava stop`: HUP/TERM, a bounded grace, then SIGKILL.

    The grace is `_TERMINAL_STOP_GRACE_S`, capped by the stop's `deadline`;
    the SIGKILL leg is bounded by `_TERMINAL_KILL_WAIT_S` and runs even when
    the grace spent the rest of the deadline — a stop that reached its
    terminal phase closes its terminals
    (docs/decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md). A shell that
    survives fails the stop, which keeps its maintenance hold. Known job
    leftovers are reported without failing the stop. A
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
        database_factory=database_factory,
        machine_name=local_machine.name(),
    )
    _report_terminal_job_leftovers(outcome)
    if any(s.role == "terminal" and live_identities([s.process]) for s in outcome.survivors):
        raise _terminals_incomplete(outcome, "terminals")
    _await_no_terminals(max(deadline, time.monotonic() + _TERMINAL_KILL_WAIT_S), "terminals")


def force_close_terminals() -> None:
    """Close this unit's terminals at `ava stop --force`: no grace, no notices.

    Force skips owner notices and drain guarantees. A surviving shell still
    fails it; known job leftovers remain diagnostics.
    """
    outcome, _reason = _close_terminals_now(0.0, _TERMINAL_KILL_WAIT_S)
    _report_terminal_job_leftovers(outcome)
    if any(s.role == "terminal" and live_identities([s.process]) for s in outcome.survivors):
        raise RuntimeError(
            f"force stop did not close terminals: {sorted({s.session for s in outcome.survivors if s.role == 'terminal'})}"
        )


def stop_data_plane(
    timeout: float,
    *,
    save: bool = True,
    notes: list[str] | None = None,
    clients: list[str] | None = None,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    producer: Callable[[], EventPipeline],
) -> list[str]:
    """Stop this home's native data plane; never stop a remote-managed plane.

    `notes` collects what the stop report must say (a Postgres shutdown that had to be
    escalated); `clients` collects the pooler's still-connected clients, reported and
    never acted on.
    """
    from cli.commands.data_plane.maintenance_stop import stop

    return stop(
        timeout,
        save=save,
        notes=notes,
        clients=clients,
        retained_children=retained_children,
        producer=producer,
    )
