"""Wedged-agent controller — detect and recover live-but-stuck agents.

Desired state: a ``running``/``idling`` agent with a live process that is making
progress. An agent with a live pid but a stale unconsumed pending inbound is
wedged — its process exists but it cannot or will not claim that inbound, so its
state is indistinguishable from a dead process. The system's heartbeat generates
exactly this pattern: a check-in inbound is delivered, the agent ignores it
(a black-holed Redis pubsub socket, a hung claim loop, the RC-1 status-CAS wedge
from the sleep/wake diagnosis), and the row stays ``running``/``idling`` with a
pending inbound forever — permanent limbo, invisible to every other detector.

This controller scans for those agents, verifies the process is alive, force-kills
it with SIGKILL (so termination_source is ``'reaper'``, not ``'exit'`` — the
reaper is resurrection-eligible), then resurrects it directly so it boots fresh
and processes its backlog. One action, machine-scoped (same placement rule as the
other restarter controllers).

**Why SIGKILL, not SIGTERM.** ``agent/lifecycle.py`` routes SIGTERM→SystemExit→
finally→POST /exited with ``termination_source='exit'``, which is explicitly
excluded from auto-resurrect. SIGKILL bypasses the agent's lifecycle handler;
the reaper stamps ``'reaper'`` on the orphaned row, which is the voluntary-death
source. This controller then resurrects it directly (bypassing
CrashResurrectController's work-kind-inbound filter, since the wedging inbound
is usually just a ``heartbeat`` which is not in ``_WORK_INBOUND_KINDS``).

**Gating**: ``wedged_agent_enabled`` (default True), scan throttle (30s, same
as the reaper), per-agent backoff (``last_wedged_check_at``, same clock shape
as ``last_resurrect_at``). Gateway-health gated like the other controllers:
a resurrect while the gateway is down would crash-loop.

Extracted as a Controller so the restarter daemon runs it alongside
RespawnController, HibernateController, and CrashResurrectController.
"""

from __future__ import annotations

import logging
import time

from psycopg_pool import ConnectionPool

from ops.agents import resurrect_agent
from ops.controllers.base import BlockScope, ReconcileResult
from ops.controllers.respawn import _gateway_healthy
from ops.pages import list_open_page_names
from shared.agents import ResurrectAlreadyAlive
from shared.config import settings
from shared.live_announce import publish_page_closed_sync
from shared.machine import MachineRole, machine_name
from shared.proc import force_kill, process_alive
from shared.timing import CONTROLLER_SCAN_INTERVAL_S

_log = logging.getLogger("ops.controllers.wedged")

# Inbound age threshold (seconds). An agent holding an unconsumed pending inbound
# this long is presumed wedged — the exec timeout is 1200s + LLM retry budget
# ~770s ≈ 2000s, rounded up for margin. Override via env
# (`AVA_WEDGED_AGENT_INBOUND_AGE_SECONDS`); the derivation is registered as a
# lattice constraint in `shared/timing.py` (WEDGED_AGE_SEC >= EXEC_NODE_TIMEOUT_S
# + LLM_RETRY_BUDGET_ESTIMATE_S), so an override that shrinks it below the
# derivation is caught at restarter startup.
#
# Scan throttle — matched to the respawn reaper's cadence
# (`shared.timing.CONTROLLER_SCAN_INTERVAL_S`). A wedged agent is not
# detectable until its pending inbound ages past the threshold, so faster scanning
# finds nothing new.

# Per-agent backoff. Once we attempt recovery of an agent, do not try again for
# this long — prevents a poison-message loop from becoming a kill-spawn cycle.
_BACKOFF_S = 600.0


def _claim_wedged_candidates(
    pool: ConnectionPool, local_machine: str, age_s: float, backoff_s: float
) -> list[tuple[int, int]]:
    """Atomically claim this host's wedged-agent candidates: ``(agent_id, pid)``.

    One UPDATE both selects and claims: on match it stamps ``last_wedged_check_at``,
    so a concurrent pass cannot double-claim the same agent. Returns the claimed
    pairs for the caller to kill+resurrect.

    Selects ``running``/``idling`` rows on this host with a live pid, holding an
    unconsumed ``pending`` inbound older than ``age_s``, and either never checked
    or past the per-agent backoff. The UPDATE's RETURNING is the atomic claim —
    the caller processes every returned row and tolerates a pid that died between
    the SELECT and the kill (force_kill is a no-op on a dead pid).
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET last_wedged_check_at = now() "
            "WHERE status IN ('running', 'idling') AND lease_expires_at > now() "
            "AND machine = %s "
            "AND pid IS NOT NULL "
            "AND (last_wedged_check_at IS NULL "
            "     OR now() - last_wedged_check_at >= make_interval(secs => %s)) "
            "AND EXISTS ("
            "  SELECT 1 FROM inbound_messages im "
            "  WHERE im.agent_id = agents_meta.id "
            "    AND im.status = 'pending' "
            "    AND im.created_at < now() - make_interval(secs => %s)"
            ") "
            "RETURNING id, pid",
            (local_machine, backoff_s, age_s),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


class WedgedAgentController:
    """Detect and recover live-but-stuck agents on this host."""

    name = "wedged"

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._last_scan: float = 0.0

    def reconcile(self, role: MachineRole) -> ReconcileResult:
        if role != "agent-runner":
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)

        if not settings.daemon.wedged_agent_enabled:
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)

        # Throttle: only scan every CONTROLLER_SCAN_INTERVAL_S.
        now = time.monotonic()
        if now - self._last_scan < CONTROLLER_SCAN_INTERVAL_S:
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)
        self._last_scan = now

        local_machine = machine_name()

        # Gateway-health gate: a resurrect while the gateway is down crash-loops.
        if not _gateway_healthy():
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)

        age_s = float(settings.daemon.wedged_agent_inbound_age_seconds)
        candidates = _claim_wedged_candidates(self._pool, local_machine, age_s, _BACKOFF_S)

        acted = False
        for agent_id, pid in candidates:
            if not process_alive(pid):
                # PID died between the SELECT and now — the reaper will handle it.
                _log.info(
                    "[ops.wedged] agent %s pid %s already dead; reaper will finalize",
                    agent_id,
                    pid,
                )
                continue

            _log.warning(
                "[ops.wedged] agent %s (pid %s) is wedged (live process, stale pending "
                "inbound > %.0fs) — force-killing and resurrecting",
                agent_id,
                pid,
                age_s,
            )

            try:
                # Force status to 'terminated' before killing — the kill itself may
                # race, and we need the row stamped before the reaper sees the dead pid.
                with self._pool.connection() as conn, conn.cursor() as cur:
                    # Capture open page names BEFORE the status flip — the
                    # cascade_close_agent_pages trigger closes the page rows on
                    # 'terminated'. The resurrect below reopens them (cascade_open)
                    # and the agent boot re-serves + re-publishes PageOpened, so the
                    # frontend ends consistent either way; if the resurrect fails the
                    # PageClosed events leave the popover correct.
                    page_names = list_open_page_names(conn, agent_id)
                    cur.execute(
                        "UPDATE agents_meta SET status = 'terminated', "
                        "termination_source = 'reaper' "
                        "WHERE id = %s AND status IN ('running', 'idling') "
                        "AND lease_expires_at > now()",
                        (agent_id,),
                    )
                    transitioned = cur.rowcount == 1
                if transitioned:
                    for page_name in page_names:
                        publish_page_closed_sync(agent_id, page_name)
                force_kill(pid)

                # Resurrect: pass a prompt so the agent knows why it was woken.
                resurrect_agent(
                    agent_id,
                    resurrected_by="system",
                    prompt=(
                        "You were force-restarted by the wedged-agent detector — "
                        "your process was alive but stuck (unconsumed pending inbound). "
                        "Continue from where you left off."
                    ),
                )
                acted = True
                _log.info(
                    "[ops.wedged] agent %s recovered: killed pid %s, resurrected", agent_id, pid
                )
            except ResurrectAlreadyAlive:
                _log.info(
                    "[ops.wedged] agent %s already alive — race with another recovery", agent_id
                )
            except Exception:
                _log.exception("[ops.wedged] failed to recover agent %s", agent_id)

        detail = f"recovered {sum(1 for _ in candidates)} agent(s)" if candidates else None
        return ReconcileResult(
            dimension=self.name, blocks=BlockScope.NONE, acted=acted, detail=detail
        )
