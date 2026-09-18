"""Pause native agents at their existing durable restart boundary.

The local pause journal closes new admission while existing actions keep their
SDK dependencies. It survives a data-plane outage; the restart itself and its
checkpoint remain in PostgreSQL. No external-agent ownership is acquired here.

Update-family drains additionally reap their stragglers (`reap=True`, task
#4016): a cohort member still un-landed W seconds after its restart command
was issued is CAS-marked 'restarting' — the durable truncation signal — and
released with the honest `reaped` outcome, never a fabricated flush receipt.
Its mark is settled at the successor boot or local resume
(`shared/straggler_reap.py`), which restores the row to runnable and lets the
ordinary reconcile re-deliver its claimed work on the new code.
"""

import logging
import math
import os
import time
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID, uuid4

from ops.agent_pause_probe import HostIdentity, host_identity, host_running
from shared import maintenance, maintenance_cohort, pause_owner
from shared.db import connect, publish_inbound_wake
from shared.hold_driver import HoldDriver
from shared.machine import machine_name, machine_role
from shared.maintenance_state import MaintenanceHold

_log = logging.getLogger(__name__)

PAUSE_TIMEOUT_SECONDS = 300.0

# The retry cadence for the bounded wait on an in-flight agent lifecycle
# command (task #3591). Every attempt re-runs cohort preparation inside its
# row-locked transaction, so the interval trades retry cost against how
# quickly a resolved command is observed.
_LIFECYCLE_WAIT_POLL_SECONDS = 5.0


def _hold(holder: str, at: datetime) -> MaintenanceHold:
    current = maintenance.require_operation(holder, at)
    assert current.maintenance is not None  # noqa: S101
    return current.maintenance


def _wake(hold: MaintenanceHold) -> None:
    for agent in hold.commands:
        publish_inbound_wake(agent, "maintenance")


def _lifecycle_wait_seconds() -> float:
    """The bounded wait when preparation meets in-flight work it did not author.

    Task #3591: 0 disables the wait (the pre-#3591 refuse-immediately
    behavior); maintenance-authored commands never wait regardless.
    """
    from shared.config import settings

    return settings.gateway.pause_lifecycle_wait_seconds


def _emit_lifecycle_wait(waited: float, outcome: str, agents: tuple[int, ...]) -> None:
    """One row per wait episode (task #3591): duration, outcome, agents waited on."""
    from shared import telemetry

    telemetry.emit(
        "telemetry",
        "pause_lifecycle_wait",
        attributes={"waited_s": round(waited, 3), "outcome": outcome, "agents": list(agents)},
    )


def _prepare(holder: str, at: datetime, *, driver: HoldDriver | None = None) -> None:
    """Publish the hold and enqueue restarts.

    `driver` is the shepherding identity of an operator-side entry (task
    #3270); daemon-driven pauses pass None and keep the handoff/outcome as
    their ownership evidence.

    In-flight work this actor did not author is bounded-waited (task #3591):
    an agent lifecycle command, and claimed ordinary work on a parked agent,
    are retried until they resolve, up to
    `settings.gateway.pause_lifecycle_wait_seconds` (0 refuses immediately),
    and only then aborts with the wait result in the message. A
    maintenance-authored command still refuses immediately.
    """
    roles = machine_role()
    identity = host_identity() if "agent-runner" in roles and host_running() else None
    # The actual running daemon, not the checked-out source, must support the
    # admission fence. First deployment of this protocol needs separate proof.
    pause_owner.begin_maintenance(holder, at, driver=driver)
    if "agent-runner" not in roles:
        with connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM agents_meta WHERE machine=%s AND status<>'terminated' LIMIT 1",
                (machine_name(),),
            ).fetchone()
        if row is not None:
            raise RuntimeError("unit has native agents but no running hosted owner")
        current = _hold(holder, at)
        if current.phase == "preparing":
            pause_owner.change_maintenance(
                holder, at, current, MaintenanceHold("draining"), refresh_driver=driver is not None
            )
        return
    hold = _prepare_cohort(holder, at, identity, driver=driver)
    if identity is not None and host_identity().owner != identity.owner:
        raise RuntimeError("agent-host changed boot during preparation; hold retained")
    _wake(hold)


def _prepare_cohort(
    holder: str, at: datetime, identity: HostIdentity | None, *, driver: HoldDriver | None
) -> MaintenanceHold:
    """Prepare the cohort, bounded-waiting out in-flight work it did not author.

    Every retry re-runs preparation — including the lifecycle-collision check —
    inside a fresh row-locked transaction under the same `(holder,
    acquired_at)` CAS, so a command that resolves during the wait cannot
    double-admit: the admission decision is always made under the locks it
    will act on (task #3591).
    """
    bound = _lifecycle_wait_seconds()
    deadline = time.monotonic() + bound
    started: float | None = None
    waited_on: tuple[int, ...] = ()
    while True:
        try:
            with connect() as conn:
                hold = maintenance_cohort.prepare(
                    conn,
                    machine=machine_name(),
                    host_owner=identity.owner if identity is not None else None,
                    holder=holder,
                    acquired_at=at,
                    host_absent=identity is None,
                    driver=driver,
                )
        except maintenance_cohort.LifecycleCollisionError as collision:
            if started is None:
                started = time.monotonic()
            waited_on = collision.agent_ids
            waited = time.monotonic() - started
            if not collision.waitable:
                _emit_lifecycle_wait(waited, "refused", waited_on)
                raise RuntimeError(
                    f"{collision}; waited {waited:.1f}s — refusing without a wait "
                    "(maintenance-authored work)"
                ) from collision
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _emit_lifecycle_wait(waited, "exceeded", waited_on)
                raise RuntimeError(
                    f"{collision}; waited {waited:.1f}s, still unfinished after the "
                    f"{bound:g}s bound — aborting (hold retained)"
                ) from collision
            time.sleep(min(_LIFECYCLE_WAIT_POLL_SECONDS, remaining))
            continue
        if started is not None:
            _emit_lifecycle_wait(time.monotonic() - started, "resolved", waited_on)
        return hold


def _straggler_reap_seconds() -> float:
    """The straggler window W in seconds; 0 disables the reap (task #4016)."""
    from shared.config import settings

    return settings.gateway.update_straggler_reap_seconds


def _reap_due(hold: MaintenanceHold, pending: list[int], window: float) -> list[int]:
    """Cohort members whose restart command has been issued for at least W.

    W is counted per member from ITS command's issuance (`created_at`, read on
    the DB clock so a retried hold and this process cannot disagree): the
    moment this wave asked the agent to restart. A member without a committed
    command (0) has not been asked yet and is never due.
    """
    ids = [hold.commands[agent] for agent in pending if hold.commands.get(agent)]
    if not ids:
        return []
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, EXTRACT(EPOCH FROM (clock_timestamp() - created_at)) "
            "FROM inbound_messages WHERE id = ANY(%s)",
            (ids,),
        ).fetchall()
    ages = {int(row[0]): float(row[1]) for row in rows}
    due: list[int] = []
    for agent in pending:
        age = ages.get(hold.commands.get(agent, 0))
        if age is not None and age >= window:
            due.append(agent)
    return due


def _reap_agent(agent: int, command_id: int) -> str:
    """CAS one straggler 'restarting' — the durable truncation mark.

    The mark alone is the kill: `agent.db.has_pending_interrupt` reads it as
    an abort signal, so the agent's in-flight exec/LLM work is truncated
    within its existing poll cadence and every old-incarnation write path is
    fenced by the status CAS predicates it leaves behind. Returns ``reaped``
    (marked, or already marked by this drain), ``moved`` (the agent landed or
    left the running state on its own), or a ``refused:<reason>`` /
    ``excluded:<reason>`` descriptor the caller classifies.
    """
    with connect() as conn, conn.transaction():
        row = conn.execute(
            "SELECT status, runtime_kind, runtime_owner, runtime_generation "
            "FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent,),
        ).fetchone()
        if row is None:
            return "refused:row-missing"
        status, kind, owner, generation = row
        if status == "restarting":
            return "reaped"
        if status != "running" or kind != "hosted" or owner is None or generation is None:
            return "moved"
        # A mark without its live signal would strand the row: the truncation
        # read (has_pending_interrupt) needs this un-applied maintenance
        # restart, and the settle face matches the same shape. If the command
        # already resolved, there is nothing to truncate — let the ordinary
        # path finish it.
        command = conn.execute(
            "SELECT 1 FROM inbound_messages WHERE id=%s AND agent_id=%s AND kind='restart' "
            "AND applied_at IS NULL AND observed_at IS NULL AND status IN ('pending','claimed') "
            "AND payload ? 'maintenance'",
            (command_id, agent),
        ).fetchone()
        if command is None:
            return "moved"
        control = conn.execute(
            "SELECT 1 FROM agent_impersonations WHERE agent_id=%s "
            "AND (status IN ('requested','accepted') "
            "OR (status='active' AND expires_at>clock_timestamp())) LIMIT 1",
            (agent,),
        ).fetchone()
        if control is not None:
            return "excluded:takeover"
        changed = conn.execute(
            "UPDATE agents_meta SET status='restarting' WHERE id=%s AND status='running' "
            "AND runtime_kind='hosted' AND runtime_owner=%s AND runtime_generation=%s",
            (agent, owner, generation),
        ).rowcount
        if changed != 1:
            return "refused:cas-lost"
    return "reaped"


def _reap_agents(
    hold: MaintenanceHold, due: list[int], window: float, *, noted_exclusions: set[int]
) -> bool:
    """Mark every due straggler, then record the honest reaped receipts.

    Marks land before any receipt: a hold that lists a member as reaped must
    never name one still in ordinary flight (receipt honesty, task #4016).
    Takeover/external-control rows are excluded (noted once per drain) and
    left to the ordinary timeout path; a refused reap aborts the drain with
    the hold retained — the pre-#4016 abort/retry fallback, never a fabricated
    reap. Returns whether any member was newly marked (a mark changes the
    hold, so the caller re-reads it).
    """
    marked: list[int] = []
    for agent in due:
        if agent in noted_exclusions or agent in hold.reaped:
            continue
        outcome = _reap_agent(agent, hold.commands[agent])
        if outcome == "reaped":
            marked.append(agent)
        elif outcome == "moved":
            noted_exclusions.add(agent)
            _log.info(
                "[pause] straggler %s left the running state before its reap mark "
                "landed; the ordinary drain path finishes it",
                agent,
            )
        elif outcome.startswith("excluded:"):
            noted_exclusions.add(agent)
            _log.info(
                "[pause] straggler %s is under external control (%s); not reaped — "
                "the ordinary timeout path applies",
                agent,
                outcome.partition(":")[2],
            )
        else:
            raise RuntimeError(
                f"straggler reap refused for agent {agent}: {outcome.partition(':')[2]} — "
                "hold retained"
            )
    for agent in marked:
        maintenance.record_reaped(agent, "update_straggler_reap")
    if marked:
        from shared import telemetry

        telemetry.emit(
            "telemetry",
            "update_straggler_reaped",
            attributes={"agents": marked, "window_s": window},
        )
        _log.warning(
            "[pause] reaped %d straggler(s) past the %gs restart window "
            "(truncated, no flush receipt): %s",
            len(marked),
            window,
            marked,
        )
    return bool(marked)


def _drain(holder: str, at: datetime, timeout: float, *, reap: bool = False) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("drain timeout must be finite and positive")
    deadline = time.monotonic() + timeout
    noted_exclusions: set[int] = set()
    # The age query dials the DB; one check per second is far inside W's
    # resolution and keeps the wait loop off the connection path.
    next_reap_check = 0.0
    while True:
        hold = _hold(holder, at)
        if hold.phase == "preparing":
            raise RuntimeError(
                "preparation is incomplete; repeat prepare or explicitly resume --cancel"
            )
        if hold.failures:
            raise RuntimeError(
                f"continuations failed; hold retained: {sorted(hold.failures)} — "
                "fix the root cause, then ava maintenance repair --operation "
                f"{holder} --acquired-at {at.isoformat()}"
            )
        if set(hold.drained) | set(hold.reaped) == set(hold.commands):
            with connect() as conn:
                maintenance_cohort.verify_drained(conn, hold)
            # Wait out the host's still-registering turns — except the reaped
            # members: their mark already released them, and the wave must not
            # stall on a truncated turn unwinding (a C-blocked one would hold
            # the whole drain; the stop leg bounds that death instead).
            if (
                "agent-runner" in machine_role()
                and host_running()
                and host_identity().active - set(hold.reaped)
            ):
                budget = deadline - time.monotonic()
                if budget <= 0:
                    raise TimeoutError("agent-host continuations did not finish before deadline")
                time.sleep(min(0.05, budget))
                continue
            if hold.phase == "draining":
                maintenance.set_phase(holder, at, "drained")
            if time.monotonic() > deadline:
                raise TimeoutError("drain verification exceeded its deadline; hold retained")
            return
        pending = sorted(set(hold.commands) - set(hold.drained) - set(hold.reaped))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reaped_note = f" (reaped this wave: {sorted(hold.reaped)})" if hold.reaped else ""
            raise TimeoutError(
                "drain timed out without force; hold retained for agents "
                f"{pending}{reaped_note}\n{_stall_report(hold, pending)}"
            )
        if reap and pending and time.monotonic() >= next_reap_check:
            window = _straggler_reap_seconds()
            if window > 0:
                next_reap_check = time.monotonic() + 1.0
                due = _reap_due(hold, pending, window)
                if due and _reap_agents(hold, due, window, noted_exclusions=noted_exclusions):
                    continue
        time.sleep(min(0.2, remaining))


def _stall_report(hold: MaintenanceHold, pending: list[int]) -> str:
    """One line per unfinished agent: delivery state, row fences and host view.

    A bare agent list leaves a consuming agent indistinguishable from one
    the host will never admit — issue #2159 spent its whole 300s timeout on
    exactly that ambiguity. The report names the facts an operator routes on: the
    restart command's delivery state, the row's owner/lease/resource facts,
    the agent's last activity, and whether the live host still sees the
    agent's turn running.
    """
    host_owner: UUID | None = None
    host_active: frozenset[int] = frozenset()
    notes: list[str] = []
    try:
        if "agent-runner" in machine_role() and host_running():
            identity = host_identity()
            host_owner, host_active = identity.owner, identity.active
        else:
            notes.append("live agent-host not observed; row facts only")
    except Exception as exc:  # diagnostics never mask the timeout
        notes.append(f"host identity unavailable: {exc}")
    commands = {agent: hold.commands[agent] for agent in pending}
    try:
        with connect() as conn:
            raw_rows = conn.execute(
                "SELECT m.status, m.runtime_kind, m.runtime_owner, "
                "m.lease_expires_at IS NOT NULL AND m.lease_expires_at > clock_timestamp(), "
                "m.incarnation_resources IS NULL, m.last_active_at, "
                "i.status, i.applied_at IS NOT NULL "
                "FROM unnest(%s::int[], %s::bigint[]) AS cohort(agent_id, command_id) "
                "LEFT JOIN agents_meta m ON m.id = cohort.agent_id "
                "LEFT JOIN inbound_messages i ON i.id = cohort.command_id "
                "AND i.agent_id = cohort.agent_id ORDER BY cohort.agent_id",
                (list(commands), list(commands.values())),
            ).fetchall()
    except Exception as exc:  # diagnostics never mask the timeout
        notes.append(f"row diagnostics unavailable: {exc}")
        return "".join(f"  {note}\n" for note in notes).rstrip("\n")
    rows = [_StallRow(*row) for row in raw_rows]
    lines = [
        _stall_line(agent, command, row, host_owner, host_active)
        for (agent, command), row in zip(commands.items(), rows, strict=True)
    ]
    lines.extend(notes)
    return "\n".join(f"  {line}" for line in lines)


class _StallRow(NamedTuple):
    """One unfinished cohort agent's row facts and command delivery state."""

    status: str | None
    kind: str | None
    runtime_owner: UUID | None
    lease_fresh: bool | None
    resources_settled: bool | None
    last_active: datetime | None
    delivery: str | None
    applied: bool | None


def _stall_line(
    agent: int,
    command: int,
    row: _StallRow,
    host_owner: UUID | None,
    host_active: frozenset[int],
) -> str:
    status = row.status
    if status is None:
        return f"agent {agent}: row missing; its restart command {command} is unreachable"
    seen = "yes" if agent in host_active else "no"
    line = (
        f"agent {agent}: command {command} {row.delivery}, "
        f"applied={'yes' if row.applied else 'no'}; "
        f"row {status} kind={row.kind} owner={row.runtime_owner} "
        f"lease_fresh={'yes' if row.lease_fresh else 'no'} "
        f"resources_settled={'yes' if row.resources_settled else 'no'} "
        f"last_active={row.last_active.isoformat() if row.last_active is not None else 'never'}; "
        f"host active={seen}"
    )
    if host_owner is not None and row.runtime_owner is not None and row.runtime_owner != host_owner:
        line += (
            f"\n  fence: runtime owned by {row.runtime_owner}, not the live boot {host_owner} — "
            "a successor host cannot certify its predecessor flushed; resolve explicitly "
            "(ava maintenance status, then resume --cancel or repair)"
        )
    return line


def pause_agents(
    timeout: float = PAUSE_TIMEOUT_SECONDS,
    *,
    driver: HoldDriver | None = None,
    reap: bool = False,
) -> None:
    """Idempotently drain this unit, leaving persistent terminals untouched.

    `driver` is minted by operator-side callers (`ava stop` / `ava pause`);
    daemon-driven callers (`spawn_update`, the update quiesce) pass None so a
    long-lived caller process never masks a dead ladder shepherd.

    `reap` enables the update straggler reap (task #4016): a cohort member
    still un-landed W seconds after its restart command's issuance is
    CAS-marked 'restarting' (truncating its in-flight turn) and released with
    the honest `reaped` outcome, instead of aborting the whole drain. Only
    update-family callers pass True; interactive pause/stop/restart drains
    keep the never-kill contract.
    """
    current = pause_owner.read()
    if current.status == "invalid":
        raise RuntimeError("cannot pause with an unreadable local pause owner")
    if current.status == "paused":
        assert current.holder is not None and current.acquired_at is not None  # noqa: S101
        holder, at = current.holder, current.acquired_at
        if driver is not None:
            pause_owner.refresh_driver(holder, at, driver=driver)
    else:
        holder, at = f"local-pause:{machine_name()}:{os.getpid()}:{uuid4()}", datetime.now(UTC)
    if (
        current.status != "paused"
        or current.maintenance is None
        or current.maintenance.phase == "preparing"
    ):
        _prepare(holder, at, driver=driver)
    hold = _hold(holder, at)
    if hold.phase in ("preparing", "draining", "drained"):
        _drain(holder, at, timeout, reap=reap)


def resume_agents() -> None:
    """Release the current local admission hold after start or an aborted drain."""
    current = maintenance.snapshot()
    if current is None:
        return
    assert current.maintenance is not None  # noqa: S101
    assert current.holder is not None and current.acquired_at is not None  # noqa: S101
    if current.maintenance.failures:
        raise RuntimeError(
            "cannot resume failed continuation/flush receipts; fix the root cause, "
            "then ava maintenance repair --operation "
            f"{current.holder} --acquired-at {current.acquired_at.isoformat()}"
        )
    pause_owner.change_maintenance(
        current.holder, current.acquired_at, current.maintenance, current.maintenance, resumed=True
    )
    _wake(current.maintenance)
