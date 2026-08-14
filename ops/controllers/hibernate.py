"""Hibernation controller — the memory swap-out / swap-in dimension.

Desired state: an agent that has sat 'idling' past `hibernate_idle_threshold_seconds`
has NO resident process (its RAM is freed, the row parked 'hibernating'); a
'hibernating' agent that has an inbound waiting HAS a fresh process (so the wake is
handled). Two reconcile actions, both machine-scoped — this host only touches its
own agents' processes (`machine = local`), the same placement rule the respawn
reapers hold:

- **swap-out**: an agent 'idling' past the threshold with no pending inbound ->
  confirm the row's pid is still that agent's own process, then send SIGUSR1,
  which the agent process converts to a clean exit that parks its row
  'hibernating' (agent/lifecycle.py: the SIGUSR1 handler -> SystemExit ->
  finally -> POST /api/agents/{id}/hibernating). Gated on `hibernate_enabled`.
  Exempts this host's `hibernate_min_active` most recently active agents (the
  warm-pool floor, ranked by `last_active_at` among 'running'/'idling' only —
  see `_select_local_swap_out_candidates`) regardless of idle time.
- **swap-in**: a 'hibernating' agent that has a pending inbound (a heartbeat
  nudge, a chat, a task) -> relaunch via `swap_in_agent` (clean restart, no
  marker). Runs ALWAYS, even when `hibernate_enabled` is off, so disabling the
  feature drains the already-hibernating agents back in as they are nudged rather
  than stranding them.

Both run every reconcile (the daemon's 1s tick). Unlike the respawn reaper — which
identity-probes every agent's pid and so is gated to 30s to keep the per-second
cost off O(fleet) — the two scans here are single indexed SELECTs (the probe +
os.kill in swap-out fire only on the few matches), the same cheap shape as the
restart-dispatch select the reaper's daemon already runs every second.

Swap-out is safe to repeat, but not for the reason first written here. The
intended mechanism is that a signalled agent leaves 'idling' within milliseconds
(the idle-wait's own finally flips it to 'running' before it parks 'hibernating'),
so the next tick's `status='idling'` scan no longer selects it. That argument
assumes the signal reached the agent — and when the row's pid had been recycled to
an unrelated process, nothing left 'idling' and the scan re-selected the same rows
every tick for as long as they sat there, firing a fresh SIGUSR1 at the bystander
each time (issue #1123, five agents, ~13 volleys each). The guard is now upstream of
the signal rather than downstream of it: `_signal_swap_out` signals only a pid the
OS confirms is that agent's process, so an unverifiable pid draws no signal at all
and the respawn reaper — which reads the same identity, not mere liveness — clears
the row within its 30s pass, ending the re-selection.

This is the unified wake path: any source that INSERTs a pending inbound for a
hibernating agent (heartbeat's raw INSERT, a chat delivery, a task fire) is picked
up by the swap-in poll — no wake source needs its own swap-in call, so a new one
is covered automatically. The cost is up to one restarter poll interval of extra
latency before the relaunch begins; a hibernating agent is by definition idle past
the threshold, so it is not in an active exchange.

Extracted as a Controller so the restarter daemon runs it in the same 1s loop as
RespawnController; like respawn it is a non-blocking, always-run reconcile
(`blocks` always `BlockScope.NONE`). A `psycopg.ProgrammingError` from a scan query
(schema/code drift) propagates so the daemon's loop can log-critical + exit; a
single agent's swap-in failure is caught per-agent so one bad row never drops the
whole pass.
"""

from __future__ import annotations

import logging
import os
import signal

from psycopg_pool import ConnectionPool

from ops.agent_identity import AgentProcessIdentity, probe_agent_process
from ops.agents import swap_in_agent
from ops.controllers.base import BlockScope, ReconcileResult
from shared.config import settings
from shared.machine import MachineRole, machine_name

_log = logging.getLogger("ops.controllers.hibernate")

# Freshness window for "pending inbound" / swap-out recency checks (seconds).
# A pending inbound older than this no longer counts as "about to wake" — the
# swap-in/swap-out decisions key on it. Same value and order of magnitude as
# `shared.deploy_timing.NO_PROGRESS_TIMEOUT_S` but a DIFFERENT quantity: that
# one judges deployment progress; this one ignores stale pending rows
# (2026-08-08 audit, P3-5 — was a bare 900.0 inline in two queries).
_PENDING_FRESH_WINDOW_S = 900.0


def _select_local_swap_out_candidates(
    pool: ConnectionPool, local_machine: str, idle_threshold_s: float, min_active: int
) -> list[tuple[int, int]]:
    """This host's swap-out candidates as `(agent_id, pid)`: 'idling' agents parked
    past `idle_threshold_s` with no pending inbound and a non-NULL pid, outside the
    `min_active` warm-pool floor. Non-NULL is all the SQL can assert — whether the
    pid still names that agent's process is settled by `_signal_swap_out`, which is
    where the OS can be asked.

    The idle clock is `last_active_at` (the last completed LLM turn) — NOT
    status_changed_at — so an ops restart never resets it and a swapped-out agent's
    heartbeat due-time (also keyed off last_active_at) is unaffected. The "no
    pending inbound" NOT EXISTS mirrors the heartbeat daemon's guard: an agent
    about to wake on a queued message must not be swapped out from under it. Only
    'idling' is selected — never 'running' — so no in-flight turn is interrupted
    (a swapped-out agent has, by definition, no dangling tool_use to repair).

    The floor: this host's 'running'/'idling' agents are ranked by `last_active_at`
    DESC (rank 1 = most recently active); only agents past rank `min_active` are
    eligible, so the N most recently active agents stay resident no matter how
    long they sit idling. The ranked set deliberately excludes every other status
    — a 'hibernating' or 'terminated' row (even one with a very recent
    `last_active_at`) never enters the window and so never occupies a floor slot
    ahead of a genuinely active agent. `min_active=0` ranks nothing out (rank
    starts at 1), so it is exactly the pre-floor, unrestricted behavior.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "WITH ranked AS ("
            "  SELECT id, pid, status, last_active_at, "
            "         row_number() OVER (ORDER BY last_active_at DESC) AS rank "
            "  FROM agents_meta "
            "  WHERE machine = %s AND status IN ('running', 'idling') "
            "  AND lease_expires_at > now()"
            ") "
            "SELECT id, pid FROM ranked "
            "WHERE status = 'idling' AND pid IS NOT NULL AND rank > %s "
            "AND now() - last_active_at > make_interval(secs => %s) "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM inbound_messages im "
            "  WHERE im.agent_id = ranked.id AND im.status = 'pending' "
            "    AND im.created_at >= now() - make_interval(secs => %s) "
            ")",
            (local_machine, min_active, idle_threshold_s, _PENDING_FRESH_WINDOW_S),
        )
        return cur.fetchall()


def _select_local_swap_in_candidates(pool: ConnectionPool, local_machine: str) -> list[int]:
    """This host's swap-in candidates: 'hibernating' agents that have a pending
    inbound waiting (a heartbeat nudge, a chat, a task) and so need a live process
    to handle it. Machine-scoped — the relaunch must happen on the agent's home
    host (boot placement gate; agent 1513 incident)."""
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM agents_meta "
            "WHERE status = 'hibernating' AND machine = %s "
            "AND EXISTS ("
            "  SELECT 1 FROM inbound_messages im "
            "  WHERE im.agent_id = agents_meta.id AND im.status = 'pending' "
            "    AND im.created_at >= now() - make_interval(secs => %s) "
            ")",
            (local_machine, _PENDING_FRESH_WINDOW_S),
        )
        return [r[0] for r in cur.fetchall()]


def _signal_swap_out(agent_id: int, pid: int) -> bool:
    """Send the hibernation swap-out signal (SIGUSR1) to a local agent process —
    but only once the OS confirms `pid` still IS that agent's process.

    Returns True if delivered, False otherwise. Paired with agent/lifecycle.py's
    SIGUSR1 handler; POSIX-only, and agent-runners are POSIX.

    The identity probe is not defensive tidiness, it is the whole point: a row's
    pid outlives the process it names, and the OS re-issues that number to
    something unrelated (wholesale after a reboot). Signalling on liveness alone
    sent SIGUSR1 — default disposition *terminate* — into whatever had inherited
    the pid, once per restarter tick for as long as the row sat there, because a
    stranger obviously never converts the signal into the hibernate exit that
    would take the row out of the scan (issue #1123). `FOREIGN` is therefore the
    one verdict worth a warning: it means the number in the row is now pointed at
    a bystander. The corpse reaper reconciles that row on its next 30s pass, which
    also ends the re-selection.

    The kill still guards ProcessLookupError/PermissionError: the process may exit
    between the probe and the signal, and that race is nobody's fault.
    """
    identity = probe_agent_process(pid, agent_id)
    if identity is AgentProcessIdentity.FOREIGN:
        _log.warning(
            "[ops.hibernate] agent %s: pid %s is no longer this agent's process (recycled) — "
            "not signalling; the respawn reaper reconciles the row",
            agent_id,
            pid,
        )
        return False
    if identity is not AgentProcessIdentity.OWNED:
        _log.debug(
            "[ops.hibernate] agent %s: pid %s not verifiable as this agent (%s) — not signalling",
            agent_id,
            pid,
            identity,
        )
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class HibernateController:
    """Runs one swap-in + swap-out pass over this host's agent rows each reconcile.
    Constructed with the restarter's shared DB pool; `reconcile` never blocks the
    tick — it is the always-run reconcile for the hibernation dimension, not a
    gate. Swap-in runs unconditionally (latency-sensitive wake path, and it must
    keep draining hibernating agents even when the feature is off); swap-out runs
    only when `hibernate_enabled` is set.
    """

    name = "hibernate"

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def reconcile(self, role: MachineRole) -> ReconcileResult:  # noqa: ARG002 — agent-runner-only, uniform Controller signature
        local_machine = machine_name()
        # Swap-in runs every reconcile and unconditionally: a wake must be handled
        # promptly, and it must keep draining already-hibernating agents even when
        # the feature is toggled off (otherwise disabling would strand them).
        acted = self._swap_in(local_machine)
        if settings.daemon.hibernate_enabled:
            acted = self._swap_out(local_machine) or acted
        return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE, acted=acted)

    def _swap_in(self, local_machine: str) -> bool:
        """Relaunch this host's hibernating agents that have a pending inbound.
        Returns True if any agent was swapped in."""
        acted = False
        for tid in _select_local_swap_in_candidates(self._pool, local_machine):
            try:
                if swap_in_agent(tid):
                    acted = True
                    _log.info(
                        "[ops.hibernate] swapped in hibernating agent %s (pending inbound)", tid
                    )
            except Exception as exc:
                _log.error("[ops.hibernate] swap-in agent %s failed: %r", tid, exc)
        return acted

    def _swap_out(self, local_machine: str) -> bool:
        """Signal swap-out of this host's idle agents parked past the threshold,
        outside the warm-pool floor. Returns True if any agent was signalled."""
        threshold = settings.daemon.hibernate_idle_threshold_seconds
        min_active = settings.daemon.hibernate_min_active
        acted = False
        for tid, pid in _select_local_swap_out_candidates(
            self._pool, local_machine, threshold, min_active
        ):
            if _signal_swap_out(tid, pid):
                acted = True
                _log.info(
                    "[ops.hibernate] signalled swap-out of idle agent %s (pid=%s, idle > %.0fs)",
                    tid,
                    pid,
                    threshold,
                )
        return acted
