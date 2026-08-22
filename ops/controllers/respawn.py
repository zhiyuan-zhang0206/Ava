"""Respawn controller — the agent-process dimension.

Desired state (from Spec, implicitly): every agent row this host owns has a live
process behind it. Drift and its heal, one reconcile pass:
- a ``restarting`` row → respawn a fresh process attached to the same agent_id
  (``respawn_agent``), gated on the gateway being healthy (a respawn that can't
  reach the gateway would crash on boot and lose the restart signal);
- an orphan row whose process is gone → force it to ``terminated`` so it stops
  masquerading as alive and becomes visible + resurrectable. Three reapers cover
  the three ways a row strands: ``starting`` / ``allocated`` (never claimed) and
  ``running`` / ``idling`` (silently dead). "Gone" is decided by process
  *identity*, not liveness — see ``_process_still_resident``; a pid the OS has
  recycled is alive without being the agent, and reading it as alive is what left
  rows stranded indefinitely (issue #1123).

Extracted verbatim from ``services.restarter.daemon``; the daemon slims to a thin
main running this controller in its fast (1s) loop. Unlike the watchdog's
controllers (a 60s manager tick), respawn is a NON-blocking, always-run reconcile
— it is not a gate, so ``blocks`` is always ``BlockScope.NONE``; ``acted`` tells Status
whether it respawned or reaped anything this pass.

**Two cadences in one controller.** Restart dispatch runs every ``reconcile`` (the
daemon's 1s tick) — a restart signal must be acted on promptly. Corpse reaping is
gated behind ``CONTROLLER_SCAN_INTERVAL_S`` (30s): each of the three reapers full-scans a
status class of local agents and identity-probes every pid (a process-table read),
which at fleet scale (100-300 agents/box) is O(fleet) work every second for a rare
condition (a silently-dead agent). Running it on a slower
cadence — a dead row lingering ~30s before it flips to 'terminated' costs nothing —
keeps the 1s dispatch cheap. The cadence state lives on the controller instance
(``_last_reap``) so the daemon loop stays a thin `reconcile()` call; the DB writes
are O(orphans), not O(agents).
"""

from __future__ import annotations

import logging
import time

import httpx
from psycopg_pool import ConnectionPool

from ops.agent_identity import RESIDENT_IDENTITIES, AgentProcessIdentity, probe_agent_process
from ops.agent_wake import revive_agent
from ops.agents import respawn_agent
from ops.controllers.base import BlockScope, ReconcileResult
from ops.pages import list_open_page_names
from shared.boot_timing import ALLOCATED_REAP_GRACE_SEC
from shared.config import settings
from shared.http_dial import get as dial_get
from shared.live_announce import publish_agent_updated_sync, publish_page_closed_sync
from shared.machine import MachineRole, machine_name
from shared.proc import force_kill
from shared.timing import CONTROLLER_SCAN_INTERVAL_S

_log = logging.getLogger("ops.controllers.respawn")

# Gateway health-check URL + timeout (seconds).
_GATEWAY_HEALTH_URL = settings.services.gateway_health_url
_GATEWAY_HEALTH_TIMEOUT_S = 2.0

# Grace before a stuck 'allocated' row is reaped — the boot family's
# ALLOCATED_REAP_GRACE_SEC (env: `AVA_ALLOCATED_REAP_GRACE_SECONDS`), whose
# ordering against the launch-confirm window is declared in `shared/timing.py`.
#
# Corpse-reaping cadence, decoupled from the per-reconcile (1s) restart dispatch —
# see the module docstring. A silently-dead row lingering an extra ~30s before it
# flips to 'terminated' costs nothing; a full-fleet pid scan every second does not.
# The 30 s cadence is shared with the wedged and crash-resurrect controllers
# (`shared.timing.CONTROLLER_SCAN_INTERVAL_S`), and its ordering against the
# agent lease renewal is declared there too.


def _gateway_healthy() -> bool:
    """Return True if the gateway health endpoint is reachable.

    Before respawning agents the restarter must confirm the gateway is up —
    otherwise every respawned agent will crash during boot (can't reach the gateway
    to claim its row and register), and the reaper will force-terminate them,
    losing the restart signal permanently.

    Fast, non-blocking check. On any failure (timeout, connection refused, non-200)
    return False and skip respawn for this tick — agents stay in 'restarting' and
    are retried next tick.
    """
    try:
        response = dial_get(
            _GATEWAY_HEALTH_URL,
            timeout=_GATEWAY_HEALTH_TIMEOUT_S,
            follow_redirects=True,
        )
        return response.status_code == 200
    except (httpx.HTTPError, TimeoutError, OSError):
        return False


def _select_local_restarting_ids(pool: ConnectionPool, local_machine: str) -> list[int]:
    """SELECT this host's 'restarting' agents — in multi-machine deployments, rows
    whose machine field is not this host are **not** touched.

    ``respawn_agent`` launches a local detached process attached to the same
    agent_id; cross-machine launch would start the agent on the wrong physical host.
    Each machine dispatches only its own home agents' restarts.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM agents_meta WHERE status = 'restarting' AND machine = %s",
            (local_machine,),
        )
        return [r[0] for r in cur.fetchall()]


def _process_still_resident(agent_id: int, pid: int) -> bool:
    """True if the row's pid should be read as "the agent is still there".

    This is the predicate both pid reapers turn on, and it is deliberately an
    identity question, not a liveness one. `agents_meta.pid` is an unleased
    integer: once the agent dies the OS may reissue that number, and after a
    reboot it reissues the whole range at once. A `process_alive` check passes on
    such a pid forever, which is how five rows sat 'idling' behind dead agents
    across a dozen reaper passes while the hibernation scan signalled the
    strangers that had inherited their pids (issue #1123). Liveness was only ever
    a proxy for "the process behind this row exists"; identity is the thing
    itself.

    False (reap) requires positive evidence — the pid is gone, or a full cmdline
    was read and it belongs to something else. `UNREADABLE` (another user's
    process, a zombie) counts as resident: this reaper is the one thing that can
    force a live agent's row to 'terminated', so it must never do that on the
    strength of a probe that could not see. That is the same safety floor the
    old PermissionError-counts-as-alive rule kept, reached the same way.
    """
    identity = probe_agent_process(pid, agent_id)
    if identity is AgentProcessIdentity.FOREIGN:
        _log.warning(
            "[ops.respawn] agent %s: pid %s now belongs to an unrelated process — the agent is "
            "gone and its pid was recycled",
            agent_id,
            pid,
        )
    return identity in RESIDENT_IDENTITIES


def _reap_local_dead_starting(pool: ConnectionPool, local_machine: str) -> list[int]:
    """Reap this host's orphan 'starting' rows whose process is gone.

    A process sets status='starting' + pid in one early-boot step and only flips to
    'running' once it enters the run loop. If it dies in between (boot crash, OOM,
    SIGKILL, schema drift), the row strands at 'starting' forever. A 'starting' row
    whose pid is no longer *this agent's process* is a confirmed orphan; force it
    to 'terminated'.

    Machine-scoped, so each pid is local and ours to probe. A pid the probe still
    identifies as this agent means it is mid-boot — left untouched. A NULL pid is
    left for manual attention.

    The UPDATE re-asserts the exact pid probed (``AND pid = %s``) to close an ABA
    window: between the SELECT and the UPDATE the row could cycle terminated ->
    allocated -> 'starting' under a fresh live pid; pinning the pid means the stale
    reap can only hit the dead process actually observed.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, pid FROM agents_meta WHERE status = 'starting' AND machine = %s",
            (local_machine,),
        )
        rows = cur.fetchall()
    reaped: list[int] = []
    for agent_id, pid in rows:
        if pid is None or _process_still_resident(agent_id, pid):
            continue
        with pool.connection() as conn, conn.cursor() as cur:
            # Capture open page names BEFORE the status flip — the
            # cascade_close_agent_pages trigger closes the page rows on
            # 'terminated', after which the open-page query matches nothing.
            page_names = list_open_page_names(conn, agent_id)
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', termination_source = 'reaper' "
                "WHERE id = %s AND status = 'starting' AND pid = %s",
                (agent_id, pid),
            )
            transitioned = cur.rowcount == 1
        if transitioned:
            with pool.connection() as conn:
                publish_agent_updated_sync(conn, agent_id)
            for page_name in page_names:
                publish_page_closed_sync(agent_id, page_name)
            reaped.append(agent_id)
    return reaped


def _reap_local_stale_allocated(
    pool: ConnectionPool, local_machine: str, grace_s: float
) -> list[int]:
    """Reap this host's 'allocated' rows stuck past ``grace_s``.

    'allocated' is the brief window between spawn/resurrect inserting the row and
    the launched process claiming it (-> 'starting'); it normally lasts the
    cold-start 1-2s. A row stuck 'allocated' well beyond that means the process died
    before claiming or was never launched. Unlike 'starting', 'allocated' carries no
    pid to probe, so the orphan test is age: status_changed_at older than ``grace_s``.

    Machine-scoped. The UPDATE re-asserts the age predicate so a row that a process
    claims (-> 'starting') or that resurrect re-stamps between the SELECT and the
    UPDATE is left alone.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM agents_meta "
            "WHERE status = 'allocated' AND machine = %s "
            "AND status_changed_at < now() - make_interval(secs => %s)",
            (local_machine, grace_s),
        )
        ids = [r[0] for r in cur.fetchall()]
    reaped: list[int] = []
    for agent_id in ids:
        with pool.connection() as conn, conn.cursor() as cur:
            # Capture open page names BEFORE the status flip — the
            # cascade_close_agent_pages trigger closes the page rows on
            # 'terminated', after which the open-page query matches nothing.
            page_names = list_open_page_names(conn, agent_id)
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', termination_source = 'reaper' "
                "WHERE id = %s AND status = 'allocated' "
                "AND status_changed_at < now() - make_interval(secs => %s)",
                (agent_id, grace_s),
            )
            transitioned = cur.rowcount == 1
        if transitioned:
            with pool.connection() as conn:
                publish_agent_updated_sync(conn, agent_id)
            for page_name in page_names:
                publish_page_closed_sync(agent_id, page_name)
            reaped.append(agent_id)
    return reaped


def _revive_local_dead_running_idling(
    pool: ConnectionPool, local_machine: str, max_revive: int
) -> list[int]:
    """Revive this host's dead 'running'/'idling' rows instead of reaping them.

    A live agent flips to 'running' on entering its run loop and to 'idling'
    while it waits for the next inbound wake-up — both mean "the process exists
    and owns this row". When that process dies (machine reboot / power-off /
    OOM / SIGKILL), the row keeps its status. The pre-G5 reaper forced such
    rows to 'terminated', and nothing ever brought the agent back — the
    heartbeat only targets idling/hibernating, crash-resurrect needs a pending
    inbound, and `ava start` revives no one: after a reboot the whole local
    fleet sat dead until the next message (Task #689 G5, the user-visible
    "machine came back, agents did not").

    This pass instead relaunches the agent in place: identity-probe the pid
    (the same safety floor as the old reaper — only positive evidence of death
    revives; UNREADABLE counts as resident), then `revive_agent` CASes the row
    to 'allocated' + launches a fresh process. The row's checkpoint survives,
    so the revived agent resumes exactly where it was. Revives are capped per
    pass (`max_revive`, AVA_REVIVE_MAX_PER_PASS) so a mass-death event cannot
    launch the whole fleet in one tick; a backlog drains over successive
    passes.

    Machine-scoped: each pid is local and ours to probe. A NULL pid is left
    for manual attention (same as the old reaper). The CAS re-asserts the
    exact probed pid + status to close the ABA window.

    Returns the ids revived this pass (for the caller's log line).
    """
    revived: list[int] = []
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, pid FROM agents_meta "
            "WHERE status IN ('running', 'idling') AND machine = %s",
            (local_machine,),
        )
        rows = cur.fetchall()
    for agent_id, pid in rows:
        if len(revived) >= max_revive:
            _log.warning(
                "[ops.respawn] revive pass hit AVA_REVIVE_MAX_PER_PASS=%s — "
                "%s dead running/idling rows deferred to the next pass",
                max_revive,
                len(rows) - len(revived),
            )
            break
        if pid is None:
            continue
        identity = probe_agent_process(pid, agent_id)
        if identity is AgentProcessIdentity.FOREIGN:
            _log.warning(
                "[ops.respawn] agent %s: pid %s now belongs to an unrelated process — "
                "the agent is gone and its pid was recycled; reviving",
                agent_id,
                pid,
            )
        elif identity is not AgentProcessIdentity.GONE:
            continue  # OWNED (alive) or UNREADABLE (no evidence of death) — safety floor
        try:
            if revive_agent(agent_id, pid):
                revived.append(agent_id)
        except Exception as exc:
            _log.error("[ops.respawn] revive agent %s failed: %r", agent_id, exc)
    return revived


def _collect_local_lease_zombies(pool: ConnectionPool, local_machine: str) -> list[int]:
    """R1 (Task #1021): 'running'/'idling' rows whose lease expired (or was
    never granted — pre-lease code) are zombies: the lease is the liveness
    authority, and a row that stopped renewing is dead even when its pid still
    answers. A resident process behind an expired lease is wedged or running
    pre-lease code — kill it; the revive pass below then relaunches the row in
    place. 'hibernating' is exempt by design (swapped out, no renewal, and the
    status set here never includes it). The pass is gated upstream by
    ``_reap_corpses``'s post-outage grace window: during a DB outage the
    paused-but-alive processes cannot renew, and killing them on the first
    post-outage reap would destroy the in-flight turn the pause exists to
    preserve (2026-08-08 audit, P1-2).

    Returns the ids acted on (killed, or already dead and left for the revive
    pass to relaunch).
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, pid FROM agents_meta "
            "WHERE status IN ('running', 'idling') AND machine = %s "
            "AND (lease_expires_at IS NULL OR lease_expires_at <= now())",
            (local_machine,),
        )
        rows = cur.fetchall()
    zombies: list[int] = []
    for agent_id, pid in rows:
        if pid is not None and _process_still_resident(agent_id, pid):
            force_kill(pid)
        zombies.append(agent_id)
    return zombies


class RespawnController:
    """Runs one respawn+reap pass over this host's agent rows. Constructed with the
    restarter's shared DB pool (the 1s poll reuses one pool); ``reconcile`` never
    blocks the tick — it is the always-run reconcile for the agent-process
    dimension, not a gate.

    ``psycopg.ProgrammingError`` from a reaper (schema/code drift) propagates out so
    the daemon's loop can log-critical + exit (retry won't self-heal); a single
    agent's ``respawn_agent`` failure is caught per-agent and logged so one bad row
    never drops the whole pass.
    """

    name = "respawn"

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._last_reap = (
            0.0  # monotonic() of the last reaper pass; 0.0 forces one on the first call
        )
        self._lease_zombie_grace_until = 0.0  # wall clock; set by the daemon (see below)

    def set_lease_zombie_grace_until(self, deadline: float) -> None:
        """Arm (or re-arm) the lease-zombie grace window at wall-clock `deadline`.

        Called by the restarter daemon every tick with the window it computed
        from its own DB-link observations (wall-clock tick gap / reconcile
        failure streak — see services/restarter/daemon.py). The controller
        reads the value when the lease-zombie pass next runs; 0.0 (never armed
        / expired) means the pass runs as usual.
        """
        self._lease_zombie_grace_until = deadline

    def reconcile(self, role: MachineRole) -> ReconcileResult:  # noqa: ARG002 — agent-runner-only, uniform Controller signature
        local_machine = machine_name()
        acted = self._dispatch_respawns(local_machine)

        # Corpse reaping runs on the slower CONTROLLER_SCAN_INTERVAL_S cadence, not every
        # reconcile: each reaper full-scans a status class and probes every pid,
        # O(fleet) work for a rare condition (see CONTROLLER_SCAN_INTERVAL_S).
        now = time.monotonic()
        if now - self._last_reap >= CONTROLLER_SCAN_INTERVAL_S:
            self._last_reap = now
            acted = (
                self._reap_corpses(local_machine, grace_until=self._lease_zombie_grace_until)
                or acted
            )

        return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE, acted=acted)

    def _dispatch_respawns(self, local_machine: str) -> bool:
        """Respawn this host's 'restarting' agents (gateway-health-gated). Runs every
        reconcile — a restart signal must be acted on promptly. Returns True if it
        attempted any respawn."""
        ids = _select_local_restarting_ids(self._pool, local_machine)
        if ids and not _gateway_healthy():
            _log.debug(
                "[ops.respawn] gateway not healthy, deferring respawn of %d agent(s)", len(ids)
            )
            return False
        for tid in ids:
            try:
                started = respawn_agent(tid)
                if started:
                    _log.info("[ops.respawn] respawned agent %s (new PID)", tid)
                else:
                    # The CAS inside respawn_agent lost — another dispatcher / a
                    # concurrent lifecycle op won. Log it so an agent that stays
                    # 'restarting' shows a respawn attempt with an outcome.
                    _log.info("[ops.respawn] respawn agent %s: no-op, lost the claim race", tid)
            except Exception as exc:
                _log.error("[ops.respawn] respawn agent %s failed: %r", tid, exc)
        return bool(ids)

    def _reap_corpses(self, local_machine: str, *, grace_until: float = 0.0) -> bool:
        """Run the orphan reapers (dead 'starting' / stale 'allocated') plus the
        dead-'running'|'idling' revive pass. `grace_until` gates ONLY the
        lease-zombie pass — the pid-based reapers and the revive pass never
        kill a live process, so they are safe to run during a post-outage
        grace window (and the revive pass is what relaunches rows whose
        processes really died in the outage). Returns True if any row was acted on."""
        acted = False
        for tid in _reap_local_dead_starting(self._pool, local_machine):
            acted = True
            _log.warning(
                "[ops.respawn] reaped orphan agent %s — 'starting' with a dead process; "
                "forced to 'terminated'",
                tid,
            )
        for tid in _reap_local_stale_allocated(self._pool, local_machine, ALLOCATED_REAP_GRACE_SEC):
            acted = True
            _log.warning(
                "[ops.respawn] reaped orphan agent %s — stuck 'allocated' past %.0fs "
                "(process died before claiming / never launched); forced to 'terminated'",
                tid,
                ALLOCATED_REAP_GRACE_SEC,
            )
        if grace_until and time.time() < grace_until:
            # Post-outage grace: every agent's lease expired while the
            # DB-outage pause kept the processes alive but unable to renew;
            # the paused processes renew within one renew interval of the DB
            # answering. Skipping the lease-zombie pass (the ONLY pass that
            # force-kills a live process) keeps them alive; the pid-based
            # reapers above and the revive pass below still run, so a process
            # that really died in the outage is still handled (2026-08-08
            # audit, P1-2).
            _log.debug(
                "[ops.respawn] lease-zombie pass skipped — DB-outage grace window "
                "active (until wall %.0f); paused agents get time to renew",
                grace_until,
            )
        else:
            for tid in _collect_local_lease_zombies(self._pool, local_machine):
                acted = True
                _log.warning(
                    "[ops.respawn] collected zombie agent %s — running/idling with an "
                    "expired (or missing) lease; process killed, revive pass relaunches it",
                    tid,
                )
        for tid in _revive_local_dead_running_idling(
            self._pool, local_machine, settings.daemon.revive_max_per_pass
        ):
            acted = True
            _log.warning(
                "[ops.respawn] revived agent %s — running/idling behind a dead or "
                "recycled pid (reboot / crash); relaunched in place",
                tid,
            )
        return acted
