"""Wedged-agent controller — detect and recover live-but-stuck agents.

Desired state: a ``running``/``idling`` agent with a live process that is making
progress. An agent with a live pid but a stale unconsumed pending inbound is
wedged — its process exists but it cannot or will not claim that inbound, so its
state is indistinguishable from a dead process. The system's heartbeat generates
exactly this pattern: a check-in inbound is delivered, the agent ignores it
(a black-holed Redis pubsub socket, a hung claim loop, the RC-1 status-CAS wedge
from the sleep/wake diagnosis), and the row stays ``running``/``idling`` with a
pending inbound forever — permanent limbo, invisible to every other detector.

A running agent can also wedge after claiming a turn, leaving no pending inbound:
chat rows remain ``claimed`` until the next boot and one-step kinds are already
``done``. The controller instead detects a running state that outlives the turn
budget without a completed LLM round. Claimed-row age alone is not a signal,
because a healthy long-lived agent legitimately retains claimed chat rows.

A separate terminated zombie shape retains a live lease, pid, and pending
``terminate`` inbound after the user has already terminated the agent. It is
identity-checked and reaped without resurrection, preserving that user intent.

This controller scans for those agents and re-checks process identity under the
agent row lock. An OWNED process is force-killed before the reaper transition
commits; FOREIGN or GONE proves the old agent is absent, so it is transitioned
without signalling; UNREADABLE provides no safe evidence and is deferred. A
successful transition is resurrected directly so it boots fresh and processes
its backlog. One action, machine-scoped (same placement rule as the other
restarter controllers).

**Why SIGKILL, not SIGTERM.** ``agent/lifecycle.py`` routes SIGTERM→SystemExit→
finally→POST /exited with ``termination_source='exit'``, which is explicitly
excluded from auto-resurrect. SIGKILL bypasses the agent's lifecycle handler;
the controller stamps ``'reaper'`` on the orphaned row, which is the involuntary-death
source. This controller then resurrects it directly (bypassing
CrashResurrectController's work-kind-inbound filter, since the wedging inbound
is usually just a ``heartbeat`` which is not in ``_WORK_INBOUND_KINDS``).

**Gating**: ``wedged_agent_enabled`` (default True), scan throttle (30s, same
as the reaper), per-agent backoff (``last_wedged_check_at``, same clock shape
as ``last_resurrect_at``), and the shared ``auto_resurrect_max_attempts`` budget
of unconsumed lifecycle rows. Live-agent recovery is gateway-health gated because
a resurrect while the gateway is down would crash-loop; terminated-zombie reaping
does not require a gateway.

Extracted as a Controller so the restarter daemon runs it alongside
RespawnController and CrashResurrectController.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from psycopg_pool import ConnectionPool

from ops.agent_identity import AgentProcessIdentity, probe_agent_process
from ops.agent_wake import AutoResurrectClaim, ResurrectClaimStaleError
from ops.agents import resurrect_agent
from ops.controllers.base import BlockScope, ReconcileResult
from ops.controllers.respawn import _gateway_healthy
from ops.pages import list_open_page_names
from shared.agents import ResurrectAlreadyAlive, TerminationSource
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.live_announce import publish_page_closed_sync
from shared.machine import MachineRole, machine_name
from shared.proc import force_kill
from shared.timing import CONTROLLER_SCAN_INTERVAL_S

_log = logging.getLogger("ops.controllers.wedged")

# Inbound age thresholds (seconds). A running agent needs the long
# `AVA_WEDGED_AGENT_INBOUND_AGE_SECONDS` threshold: it covers the exec timeout
# plus LLM retry budget and is constrained by the `WEDGED_AGE_SEC` timing
# lattice. An idling agent instead uses the short
# `AVA_WEDGED_IDLING_AGENT_INBOUND_AGE_SECONDS` threshold, which covers several
# idle claim-loop fallback checks and is independent of turn work budgets.
#
# Scan throttle — matched to the respawn reaper's cadence
# (`shared.timing.CONTROLLER_SCAN_INTERVAL_S`). A wedged agent is not
# detectable until its pending inbound ages past the threshold, so faster scanning
# finds nothing new.

# Per-agent backoff. Once we attempt recovery of an agent, do not try again for
# this long — prevents a poison-message loop from becoming a kill-spawn cycle.
_BACKOFF_S = 600.0


def _claim_wedged_candidates(
    pool: ConnectionPool,
    local_machine: str,
    running_age_s: float,
    idling_age_s: float,
    backoff_s: float,
) -> list[tuple[int, int, datetime]]:
    """Atomically claim wedged candidates: ``(agent_id, pid, claim_time)``.

    One UPDATE both selects and claims: on match it stamps ``last_wedged_check_at``,
    so a concurrent pass cannot double-claim the same agent. Returns the claimed
    rows for the caller to re-check under lock and recover.

    Selects ``running``/``idling`` rows on this host with a recorded pid. A
    running row needs either an unconsumed ``pending`` inbound older than the
    long threshold or a turn older than that threshold with no completed LLM
    round. An idling row must have remained idling past the short threshold
    and needs either a pending inbound past that threshold or a non-NULL stale
    claim-loop marker. The idling-duration guard prevents an already-old inbound
    from falsely reaping a healthy turn in its running→idling→first-claim window.
    Both branches remain subject to the per-agent backoff. The UPDATE's RETURNING is the atomic claim —
    the caller processes every returned row through the OWNED/FOREIGN/GONE/
    UNREADABLE identity matrix under the agent row lock. Each wedged recovery
    inserts its own ``kind='resurrect'`` inbound, so the shared unconsumed-attempt
    budget caps the kill + prompt + resurrect loop too.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET last_wedged_check_at = now() "
            "WHERE status IN ('running', 'idling') AND lease_expires_at > now() "
            "AND machine = %s "
            "AND pid IS NOT NULL "
            "AND (last_wedged_check_at IS NULL "
            "     OR now() - last_wedged_check_at >= make_interval(secs => %s)) "
            "AND (SELECT count(*) FROM inbound_messages lc "
            "     WHERE lc.agent_id = agents_meta.id "
            "       AND lc.kind = 'resurrect' AND lc.status = 'pending') < %s "
            "AND ("
            "  (agents_meta.status = 'running' AND EXISTS ("
            "    SELECT 1 FROM inbound_messages im "
            "    WHERE im.agent_id = agents_meta.id "
            "      AND im.status = 'pending' "
            "      AND im.created_at < now() - make_interval(secs => %s)"
            "  )) "
            "  OR (agents_meta.status = 'running' "
            "      AND agents_meta.status_changed_at "
            "          < now() - make_interval(secs => %s) "
            "      AND COALESCE(agents_meta.last_active_at, agents_meta.started_at, "
            "                   agents_meta.status_changed_at) "
            "          < now() - make_interval(secs => %s)"
            "  ) "
            "  OR (agents_meta.status = 'idling' "
            "      AND agents_meta.status_changed_at "
            "          < now() - make_interval(secs => %s) "
            "      AND ("
            "        EXISTS ("
            "          SELECT 1 FROM inbound_messages im "
            "          WHERE im.agent_id = agents_meta.id "
            "            AND im.status = 'pending' "
            "            AND im.created_at < now() - make_interval(secs => %s)"
            "        ) "
            "        OR (agents_meta.last_claim_loop_at IS NOT NULL "
            "            AND agents_meta.last_claim_loop_at "
            "                < now() - make_interval(secs => %s))"
            "      )"
            "  )"
            ") "
            "RETURNING id, pid, last_wedged_check_at",
            (
                local_machine,
                backoff_s,
                settings.daemon.auto_resurrect_max_attempts,
                running_age_s,
                running_age_s,
                running_age_s,
                idling_age_s,
                idling_age_s,
                idling_age_s,
            ),
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def _claim_terminated_lease_zombies(
    pool: ConnectionPool, local_machine: str, backoff_s: float
) -> list[tuple[int, int]]:
    """Claim terminated rows whose old process still owns a live lease.

    A force termination can record the user's intent while a wedged process
    survives long enough to renew its lease. A pending terminate inbound makes
    that contradictory state explicit. The claimed row is reaped only; unlike
    ordinary wedged recovery it must never resurrect the user-terminated agent.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET last_wedged_check_at = now() "
            "WHERE status = 'terminated' AND lease_expires_at > now() "
            "AND machine = %s AND pid IS NOT NULL "
            "AND (last_wedged_check_at IS NULL "
            "     OR now() - last_wedged_check_at >= make_interval(secs => %s)) "
            "AND EXISTS ("
            "  SELECT 1 FROM inbound_messages im "
            "  WHERE im.agent_id = agents_meta.id "
            "    AND im.status = 'pending' AND im.kind = 'terminate'"
            ") "
            "RETURNING id, pid",
            (local_machine, backoff_s),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def _reap_terminated_lease_zombie(pool: ConnectionPool, agent_id: int, pid: int) -> bool:
    """Identity-check and reap a terminated agent's still-live process.

    The terminated state is the user's lifecycle intent, so this deliberately
    clears the stale process projection without changing `termination_source`
    or launching a replacement.
    """
    first_identity = probe_agent_process(pid, agent_id)
    if first_identity is AgentProcessIdentity.UNREADABLE:
        _log.info(
            "[ops.wedged] terminated agent %s pid %s identity unreadable; deferring",
            agent_id,
            pid,
        )
        return False

    try:
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, pid, lease_expires_at > now() "
                "FROM agents_meta WHERE id = %s FOR UPDATE",
                (agent_id,),
            )
            row = cur.fetchone()
            locked_identity = probe_agent_process(pid, agent_id)
            if (
                row is None
                or row[0] != "terminated"
                or row[1] != pid
                or row[2] is not True
                or locked_identity is AgentProcessIdentity.UNREADABLE
            ):
                return False
            cur.execute(
                "UPDATE agents_meta SET pid = NULL, lease_expires_at = NULL "
                "WHERE id = %s AND status = 'terminated' "
                "AND pid = %s AND lease_expires_at > now()",
                (agent_id, pid),
            )
            if cur.rowcount != 1:
                return False
            if locked_identity is AgentProcessIdentity.OWNED:
                force_kill(pid)
        _log.warning(
            "[ops.wedged] reaped terminated agent %s zombie pid %s (identity %s); "
            "preserved termination without resurrection",
            agent_id,
            pid,
            locked_identity.value,
        )
        return True
    except Exception:
        _log.exception("[ops.wedged] failed to reap terminated zombie agent %s", agent_id)
        return False


def _recover_wedged_candidate(
    pool: ConnectionPool,
    agent_id: int,
    pid: int,
    claimed_at: datetime,
    running_age_s: float,
    idling_age_s: float,
) -> bool:
    """Identity-check, reap, then resurrect one non-terminal wedge candidate.

    A pending terminate changes the last step: it is a durable user lifecycle
    intent, so the process projection is cleared and the row stays terminated
    rather than being resurrected to process unrelated backlog.
    """
    first_identity = probe_agent_process(pid, agent_id)
    if first_identity is AgentProcessIdentity.UNREADABLE:
        # No evidence licenses either a signal or a duplicate launch. Leave the
        # claimed row for a later scan; the detail count must not call this a
        # recovered agent.
        _log.info(
            "[ops.wedged] agent %s pid %s identity unreadable; deferring",
            agent_id,
            pid,
        )
        return False

    _log.warning(
        "[ops.wedged] agent %s (recorded pid %s) has a stale work signal "
        "beyond its status-aware threshold (running %.0fs, idling %.0fs) "
        "— reconciling process identity",
        agent_id,
        pid,
        running_age_s,
        idling_age_s,
    )

    try:
        transition_row = None
        page_names: list[str] = []
        pending_terminate = False
        with write_transaction(pool) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, pid, lease_expires_at > now() "
                "FROM agents_meta WHERE id = %s FOR UPDATE",
                (agent_id,),
            )
            locked_row = cur.fetchone()
            locked_identity = probe_agent_process(pid, agent_id)
            if (
                locked_row is not None
                and locked_row[0] in ("running", "idling")
                and locked_row[1] == pid
                and locked_row[2] is True
                and locked_identity is not AgentProcessIdentity.UNREADABLE
            ):
                cur.execute(
                    "SELECT EXISTS("
                    "  SELECT 1 FROM inbound_messages "
                    "  WHERE agent_id = %s AND status = 'pending' AND kind = 'terminate'"
                    ")",
                    (agent_id,),
                )
                pending_terminate_row = cur.fetchone()
                pending_terminate = (
                    False if pending_terminate_row is None else bool(pending_terminate_row[0])
                )
                # Capture cascade-closable show() pages only after the row lock
                # proves this is still the claimed live incarnation.
                page_names = list_open_page_names(conn, agent_id)
                if pending_terminate:
                    cur.execute(
                        "UPDATE agents_meta SET status = 'terminated', "
                        "termination_source = 'reaper', pid = NULL, "
                        "lease_expires_at = NULL "
                        "WHERE id = %s AND status IN ('running', 'idling') "
                        "AND pid = %s AND lease_expires_at > now() "
                        "RETURNING status_changed_at",
                        (agent_id, pid),
                    )
                else:
                    cur.execute(
                        "UPDATE agents_meta SET status = 'terminated', "
                        "termination_source = 'reaper' "
                        "WHERE id = %s AND status IN ('running', 'idling') "
                        "AND pid = %s AND lease_expires_at > now() "
                        "RETURNING status_changed_at",
                        (agent_id, pid),
                    )
                transition_row = cur.fetchone()
                if transition_row is not None and locked_identity is AgentProcessIdentity.OWNED:
                    # Keep the row lock through the identity-verified kill. A
                    # concurrent user force waits, then writes its newer
                    # fence/source after this transaction.
                    force_kill(pid)
        if transition_row is None:
            _log.info(
                "[ops.wedged] agent %s changed before recovery transition; skipping",
                agent_id,
            )
            return False
        for page_name in page_names:
            publish_page_closed_sync(agent_id, page_name)

        if pending_terminate:
            _log.info(
                "[ops.wedged] agent %s reaped for its pending terminate inbound",
                agent_id,
            )
            return True

        # Resurrect: pass a prompt so the agent knows why it was woken.
        resurrect_agent(
            agent_id,
            resurrected_by="system",
            prompt=(
                "You were restarted by the wedged-agent detector after a "
                "stale work signal showed no progress. "
                "Continue from where you left off."
            ),
            auto_claim=AutoResurrectClaim(
                agent_id=agent_id,
                termination_source=TerminationSource.REAPER,
                termination_epoch=transition_row[0],
                claim_kind="wedged",
                claimed_at=claimed_at,
            ),
        )
        _log.info(
            "[ops.wedged] agent %s recovered from process identity %s; resurrected",
            agent_id,
            locked_identity.value,
        )
        return True
    except (ResurrectAlreadyAlive, ResurrectClaimStaleError):
        _log.info("[ops.wedged] agent %s already alive — race with another recovery", agent_id)
    except Exception:
        _log.exception("[ops.wedged] failed to recover agent %s", agent_id)
    return False


class WedgedAgentController:
    """Detect and recover live-but-stuck agents on this host."""

    name = "wedged"

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._last_scan: float = 0.0

    def reconcile(self, role: MachineRole) -> ReconcileResult:
        from ops import runner_mode

        if runner_mode.is_hosted():
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)
        if role != "agent-runner":
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)

        if not settings.daemon.wedged_agent_enabled:
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)

        from shared import start_serving

        # Throttle: only scan every CONTROLLER_SCAN_INTERVAL_S.
        now = time.monotonic()
        if now - self._last_scan < CONTROLLER_SCAN_INTERVAL_S:
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)
        self._last_scan = now

        local_machine = machine_name()

        running_age_s = float(settings.daemon.wedged_agent_inbound_age_seconds)
        idling_age_s = float(settings.daemon.wedged_idling_agent_inbound_age_seconds)
        # A live-agent recovery ends in a resurrect, so it must wait for a
        # reachable gateway. A user-terminated zombie only needs reaping and
        # runs below without that dependency.
        recovered_count = 0
        with start_serving.recovery_permitted() as permitted:
            candidates = []
            if permitted and _gateway_healthy():
                candidates = _claim_wedged_candidates(
                    self._pool, local_machine, running_age_s, idling_age_s, _BACKOFF_S
                )
        for agent_id, pid, claimed_at in candidates:
            with start_serving.recovery_permitted() as permitted:
                if not permitted:
                    break
                if _recover_wedged_candidate(
                    self._pool,
                    agent_id,
                    pid,
                    claimed_at,
                    running_age_s,
                    idling_age_s,
                ):
                    recovered_count += 1

        zombie_candidates = _claim_terminated_lease_zombies(self._pool, local_machine, _BACKOFF_S)
        for agent_id, pid in zombie_candidates:
            if _reap_terminated_lease_zombie(self._pool, agent_id, pid):
                recovered_count += 1

        detail = f"recovered {recovered_count} agent(s)" if recovered_count else None
        return ReconcileResult(
            dimension=self.name,
            blocks=BlockScope.NONE,
            acted=bool(recovered_count),
            detail=detail,
        )
