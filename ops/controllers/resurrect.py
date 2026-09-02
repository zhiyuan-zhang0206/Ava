"""Crash auto-resurrect controller — the "a crashed agent still has work waiting"
dimension.

Desired state: an agent that died INVOLUNTARILY (its process crashed and the
restarter's corpse reaper forced the row 'terminated' with
``termination_source='reaper'``, OR a resurrect/respawn/spawn launch never came up and
the row was forced 'terminated' with ``termination_source='launch-confirm'`` — by the
launcher's confirm poll, or by the child's own early-boot gate) and still
has a work inbound in hand — one queued 'pending' OR one it had 'claimed' and was
processing when it died — has a fresh process to handle that inbound. One reconcile
action, machine-scoped (this host only resurrects its own agents, the same placement
rule the respawn reaper holds):

- **crash-resurrect**: a local 'terminated' row whose ``termination_source`` is one
  of the two involuntary sources, with a 'pending' or 'claimed' inbound (excluding
  pure control-signal kinds), past its per-agent backoff window, is brought back via
  ``resurrect_agent`` (the same synchronous primitive the restart path
  launch through — the gateway's async ``resurrect_if_terminated`` exists to forward
  a resurrect to a *remote*-homed agent's machine, but this scan is machine-scoped,
  so the local sync call it wraps is what runs here, exactly as RespawnController
  calls ``respawn_agent``).

Why this exists: the gateway's ``resurrect_if_terminated`` only fires when a NEW
inbound is delivered (a chat, a task fire, a compact). A crash leaves the messages
already queued pending — and the one being processed when it died sitting 'claimed'
— and if no further message ever arrives, that queue strands unboundedly: the crash
is never reconciled. Matching 'claimed' too is what closes the specific hole where
the ONLY work left is the in-flight message (no other 'pending' behind it): without
it, that agent never revives and its claimed row is never reconciled (reconciliation
only runs when a fresh process boots). The revived process's startup reconcile then
adjudicates that claimed row — committed → 'done' (no re-delivery), else →
'pending' (re-delivered) — so no message is double-processed. This controller is the
always-on backstop: any involuntary-death corpse with waiting work is reconciled on
the restarter's own loop, no external event required.

**Involuntary vs. intentional death.** The model carries an explicit
``termination_source`` (stamped by EVERY code path that writes status='terminated'),
not a heuristic. That "every" is the load-bearing part: a corpse whose source is
NULL can never be claimed by the UPDATE below, so a write site that forgets to
stamp silently strands whatever work the agent had queued, permanently. The
invariant is therefore enforced, not just documented:
``scripts/lint_termination_source.py`` fails any terminated-write whose statement
does not also set the source, and ``shared.agents.TerminationSource`` (locked to
the column's CHECK by ``tests/test_db_check_enum_sync.py``) owns the value set.

This controller resurrects ONLY ``'reaper'`` (a live agent whose process died —
OOM/SIGKILL/crash) and ``'launch-confirm'`` (a wake that never came up: the
launcher's confirm poll timed out, or the child's own early-boot schema/placement
gate rejected the boot before it claimed the row). Both are involuntary AND
self-healing — the cause clears without touching the row — which is what makes a
retry worth spacing rather than suppressing. It never resurrects ``'user'``
(force-kill / a terminate of an already-dead agent — the user's will), ``'exit'``
(the agent's own graceful process exit / caught signal), ``'integrity'`` (the
framework killed a row whose own state was self-inconsistent — corrupt history, so
ops looks before it retries), or a NULL source (a pre-column legacy row — the
conservative default). So a deliberately-killed agent stays dead even with a queued
message, and the rollout is safe by construction: only rows stamped after this code
lands are ever eligible.

**Pending-inbound filter.** An explicit ALLOWLIST of work-bearing kinds
(``_WORK_INBOUND_KINDS`` = ``chat`` + ``compact_request``), not a terminate/cancel
blacklist — fail-closed, so a future inbound kind does not silently start waking
corpses. These are exactly the kinds a live delivery resurrects for; every other
kind (a control signal, a lifecycle marker, a self-generated artifact, a heartbeat
nudge) is not work that justifies reviving a dead agent to run it. A corpse whose
only pending inbound is a ``terminate`` (the force path inserts one for audit) is
therefore never revived to process its own kill signal.

**Backoff and attempt budget (churn control).** After each resurrect the controller stamps
``last_resurrect_at`` and will not resurrect that agent again until
``auto_resurrect_backoff_seconds`` has passed (a persistent, per-agent clock — the
pin-heal backoff shape). So a resurrect that keeps failing — an outage that
re-crashes the agent on boot, a poison message that reliably kills it — is retried
at most once per window (each a loud WARN) instead of a tight loop. The continuous
crash-resurrect scan stops after ``auto_resurrect_max_attempts`` unconsumed
``kind='resurrect'`` lifecycle inbounds; a successful boot consumes those rows, so
the count represents consecutive failed recovery attempts. New post-death work still
wakes the agent through the delivery path or delivery watchdog, so a permanently
boot-failing corpse stops storming while a genuinely wanted agent is woken by a fresh
delivery. The one-shot boot revive remains outside this continuous recovery budget.
A one-off crash is recovered on the next scan (its ``last_resurrect_at`` is NULL or
long-stale). The claim + backoff stamp is a single atomic UPDATE, so under any race
only one pass resurrects a given corpse.

**Gateway-health gated**, like RespawnController: a resurrected agent self-fetches
its config from the gateway at boot, so resurrecting while the gateway is down would
crash-loop it. When the gateway is unhealthy the pass is deferred BEFORE the claim
(nothing is stamped) — the corpses stay eligible and are retried once it is back.

Extracted as a Controller so the restarter daemon runs it in the same loop as
RespawnController + WedgedAgentController; like them it is a non-blocking, always-run
reconcile (``blocks`` always ``BlockScope.NONE``). The scan is throttled to the reaper's
cadence (``shared.timing.CONTROLLER_SCAN_INTERVAL_S``, 30s): a corpse is not even marked until the reaper's
own 30s pass, so scanning faster finds nothing new, and it keeps the per-second loop
cheap. A ``psycopg.ProgrammingError`` from the scan (schema/code drift) propagates so
the daemon's loop can log-critical + exit; a single agent's resurrect failure is
caught per-agent so one bad row never drops the whole pass.
"""

from __future__ import annotations

import logging
import time

from psycopg_pool import ConnectionPool

from ops.agent_wake import (
    AutoResurrectClaim,
    ResurrectClaimStaleError,
)
from ops.agents import resurrect_agent
from ops.controllers.base import BlockScope, ReconcileResult
from ops.controllers.respawn import _gateway_healthy
from shared.agents import ResurrectAlreadyAlive, TerminationSource
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.machine import MachineRole, machine_name
from shared.timing import CONTROLLER_SCAN_INTERVAL_S

_log = logging.getLogger("ops.controllers.resurrect")

# resurrect_if_terminated's source string, reused here: the resurrect is
# framework-initiated, not a peer/user, so the lifecycle marker reads "resurrected
# by system". Must be a legal envelope source (it is — the delivery path uses it).
_RESURRECT_SOURCE = "system"

# The termination sources that mean "died involuntarily, bring it back" — owned by
# the enum (shared.agents.TerminationSource), not restated here, so adding a source
# forces a decision about its resurrect policy at the one place that defines it.
_RESURRECTABLE_SOURCES = tuple(s.value for s in TerminationSource.resurrectable())

# The inbound kinds that count as "work worth reviving a crashed agent for" — an
# explicit ALLOWLIST, not a terminate/cancel blacklist, so it is fail-closed: a new
# kind added later does NOT silently start waking corpses. These are exactly the
# kinds a live delivery resurrects for — 'chat' (a user/peer message; task fires
# deliver as chat too, and resurrect_if_terminated runs on the chat-delivery path)
# and 'compact_request' (a user/admin compact, which also runs resurrect_if_terminated).
# Every other kind is a control signal (terminate / cancel / restart), a lifecycle
# marker (resurrect / restart_completed / fork), a self-generated artifact
# (compact_summary), or a nudge (heartbeat) — none justifies reviving a dead agent
# specifically to run it.
_WORK_INBOUND_KINDS = ("chat", "compact_request")

# Scan throttle — matched to the respawn reaper's cadence. A corpse is not stamped
# 'reaper' until the reaper's own 30s pass, so a faster scan would find nothing new.


def _claim_crash_resurrect_candidates(
    pool: ConnectionPool, local_machine: str, backoff_s: float
) -> list[AutoResurrectClaim]:
    """Atomically claim crash-resurrect candidates and return their CAS tokens.

    One UPDATE both selects and claims: it stamps ``last_resurrect_at = now()`` on —
    and so RETURNs — every local 'terminated' row that (a) has an involuntary
    ``termination_source``, (b) has a 'pending' or 'claimed' inbound that is real
    work (not a terminate/cancel control signal), and (c) is past its backoff window
    (``last_resurrect_at`` NULL or older than ``backoff_s``). Doing the claim as the
    scan means two things at once: the backoff clock is advanced exactly when we
    decide to resurrect (so a resurrect that later fails is still spaced), and a
    concurrent pass cannot double-claim the same corpse (the second UPDATE matches 0
    rows for it). Machine-scoped — a resurrect launches a local process, so a foreign
    row is never touched. A corpse at ``auto_resurrect_max_attempts`` unconsumed
    ``kind='resurrect'`` lifecycle inbounds is outside the claim budget."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET last_resurrect_at = now() "
            "WHERE status = 'terminated' "
            "AND termination_source = ANY(%s) "
            "AND machine = %s "
            "AND (last_resurrect_at IS NULL "
            "     OR now() - last_resurrect_at >= make_interval(secs => %s)) "
            "AND (SELECT count(*) FROM inbound_messages lc "
            "     WHERE lc.agent_id = agents_meta.id "
            "       AND lc.kind = 'resurrect' AND lc.status = 'pending') < %s "
            "AND EXISTS ("
            "  SELECT 1 FROM inbound_messages im "
            "  WHERE im.agent_id = agents_meta.id "
            "    AND im.status IN ('pending', 'claimed') "
            "    AND im.kind = ANY(%s)"
            ") "
            "RETURNING id, termination_source, status_changed_at, last_resurrect_at",
            (
                list(_RESURRECTABLE_SOURCES),
                local_machine,
                backoff_s,
                settings.daemon.auto_resurrect_max_attempts,
                list(_WORK_INBOUND_KINDS),
            ),
        )
        claims = [
            AutoResurrectClaim(
                agent_id=r[0],
                termination_source=TerminationSource(r[1]),
                termination_epoch=r[2],
                claim_kind="crash",
                claimed_at=r[3],
            )
            for r in cur.fetchall()
        ]
        conn.commit()
    return claims


def _claim_boot_revive_candidates(
    pool: ConnectionPool, local_machine: str, backoff_s: float, max_revive: int
) -> list[AutoResurrectClaim]:
    """Atomically claim this host's boot-revive candidates and return their ids.

    The machine-reboot half of the involuntary-death story (Task #694 G5): a
    rebooted host's fleet is revived by the running/idling revive pass
    (RespawnController), but corpses already forced to 'terminated' -- the
    boot-phase/dead-birth reapers, or legacy reaps predating the revive pass --
    only ever came back when a NEW inbound arrived (the delivery-path
    resurrect) or when one was already queued (CrashResurrectController). A
    resident agent reaped with no pending work stayed dead across reboots
    forever. This pass resurrects local involuntary corpses WITHOUT a pending
    inbound requirement, exactly once per daemon start (the boot revive), so a
    machine reboot brings the whole involuntary-dead set back, not just the
    ones with queued work.

    Same shape as the crash-resurrect claim -- one UPDATE both selects and
    claims (stamps last_resurrect_at, backoff-spaced, machine-scoped,
    resurrectable sources only) -- plus a per-pass cap (`max_revive`): a
    mass-death event (a whole host's corpses) drains over boots/passes instead
    of launching the fleet in one tick. The cap rides `id IN (SELECT ... ORDER
    BY id LIMIT %s)`, so the claim stays a single atomic statement.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET last_resurrect_at = now() "
            "WHERE status = 'terminated' "
            "AND termination_source = ANY(%s) "
            "AND machine = %s "
            "AND (last_resurrect_at IS NULL "
            "     OR now() - last_resurrect_at >= make_interval(secs => %s)) "
            "AND id IN ("
            "  SELECT id FROM agents_meta "
            "  WHERE status = 'terminated' "
            "    AND termination_source = ANY(%s) "
            "    AND machine = %s "
            "    AND (last_resurrect_at IS NULL "
            "         OR now() - last_resurrect_at >= make_interval(secs => %s)) "
            "  ORDER BY id "
            "  LIMIT %s"
            ") "
            "RETURNING id, termination_source, status_changed_at, last_resurrect_at",
            (
                list(_RESURRECTABLE_SOURCES),
                local_machine,
                backoff_s,
                list(_RESURRECTABLE_SOURCES),
                local_machine,
                backoff_s,
                max_revive,
            ),
        )
        claims = [
            AutoResurrectClaim(
                agent_id=r[0],
                termination_source=TerminationSource(r[1]),
                termination_epoch=r[2],
                claim_kind="crash",
                claimed_at=r[3],
            )
            for r in cur.fetchall()
        ]
        conn.commit()
    return claims


class CrashResurrectController:
    """Runs one crash-resurrect pass over this host's involuntary-death corpses,
    throttled to ``CONTROLLER_SCAN_INTERVAL_S``. Constructed with the restarter's shared DB
    pool; ``reconcile`` never blocks the tick — it is the always-run reconcile for the
    crash-recovery dimension, not a gate. Runs only when ``auto_resurrect_enabled``.

    ``psycopg.ProgrammingError`` from the scan (schema/code drift) propagates so the
    daemon's loop can log-critical + exit; a single agent's ``resurrect_agent``
    failure is caught per-agent and logged so one bad row never drops the pass.
    """

    name = "crash_resurrect"

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
        self._last_scan = 0.0  # monotonic() of the last scan; 0.0 forces one on the first call
        self._boot_pass_done = False  # the one-shot boot revive has not run yet

    def reconcile(self, role: MachineRole) -> ReconcileResult:  # noqa: ARG002 — agent-runner-only, uniform Controller signature
        if not settings.daemon.auto_resurrect_enabled:
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE, acted=False)
        # One-shot boot revive: on the daemon's FIRST reconcile (i.e. once per
        # restarter start, which `ava start` / `ava cluster update` both run),
        # resurrect this host's involuntary corpses with no pending-inbound
        # requirement (Task #694 G5 — the machine-reboot fleet restore). Runs
        # before the 30s-throttled crash scan so a rebooted host's whole dead
        # set comes back immediately, not after the first throttle window. If
        # the gateway is down the pass defers (nothing is stamped, no backoff
        # burned) and retries on the next tick; the flag only sets once a pass
        # has actually run, so a boot-time outage cannot strand the corpses.
        if not self._boot_pass_done:
            # Gateway health decided HERE, at the flag site: with the gateway
            # down the pass is skipped WITHOUT setting the flag, so the next
            # tick retries the boot revive (nothing is stamped, no backoff
            # burned) instead of giving up on it.
            acted = False
            if _gateway_healthy():
                acted = self._resurrect_boot_corpses(machine_name())
                self._boot_pass_done = True
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE, acted=acted)
        now = time.monotonic()
        if now - self._last_scan < CONTROLLER_SCAN_INTERVAL_S:
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE, acted=False)
        self._last_scan = now
        acted = self._resurrect_crashed(machine_name())
        return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE, acted=acted)

    def _resurrect_boot_corpses(self, local_machine: str) -> bool:
        """One-shot boot revive: claim + resurrect this host's involuntary
        corpses WITHOUT a pending-inbound requirement, capped per pass. Returns
        True if any resurrect was attempted.

        Gateway-health gated exactly like the crash scan (deferred, not
        stamped, while the gateway is down). The cap is the same anti-storm
        guard the running/idling revive pass uses (`revive_max_per_pass`): a
        mass-death event drains over successive daemon starts, never as a
        burst."""
        if not _gateway_healthy():
            return False
        claims = _claim_boot_revive_candidates(
            self._pool,
            local_machine,
            settings.daemon.auto_resurrect_backoff_seconds,
            settings.daemon.revive_max_per_pass,
        )
        if not claims:
            return False
        for claim in claims:
            _log.warning(
                "[ops.crash_resurrect] boot-reviving involuntary-death agent %s "
                "(no pending inbound; machine-reboot fleet restore)",
                claim.agent_id,
            )
        return self._resurrect_claims(claims)

    def _resurrect_crashed(self, local_machine: str) -> bool:
        """Claim + resurrect this host's involuntary-death corpses that have pending
        work past their backoff. Returns True if any resurrect was attempted.

        Gateway health is checked BEFORE the claim: a resurrect boots against the
        gateway, so with it down we defer the whole pass without stamping
        last_resurrect_at (the corpses stay eligible, retried once the gateway is
        back) rather than burning a backoff window on a resurrect that would just
        crash-loop the agent."""
        if not _gateway_healthy():
            # Cheap pre-check so we don't advance the backoff clock during an outage.
            # A rare false-negative just delays one scan; the corpses are unchanged.
            return False
        claims = _claim_crash_resurrect_candidates(
            self._pool, local_machine, settings.daemon.auto_resurrect_backoff_seconds
        )
        return self._resurrect_claims(claims)

    def _resurrect_claims(self, claims: list[AutoResurrectClaim]) -> bool:
        """Resurrect claimed agents one at a time; a single agent's failure is
        caught and logged so one bad row never drops the pass. The backoff clock
        is already stamped by the claim, so a failed resurrect is spaced by the
        window, not retried next tick."""
        if not claims:
            return False
        for claim in claims:
            tid = claim.agent_id
            try:
                resurrect_agent(
                    tid,
                    resurrected_by=_RESURRECT_SOURCE,
                    auto_claim=claim,
                )
                _log.warning(
                    "[ops.crash_resurrect] auto-resurrected involuntary-death agent %s "
                    "(will not retry for %.0fs)",
                    tid,
                    settings.daemon.auto_resurrect_backoff_seconds,
                )
            except (ResurrectAlreadyAlive, ResurrectClaimStaleError):
                # Raced with another resurrect after the claim — harmless (the agent
                # is already coming back); the stamped backoff just spaces a future one.
                _log.info(
                    "[ops.crash_resurrect] agent %s already alive — another path "
                    "resurrected it first",
                    tid,
                )
            except Exception as exc:
                # resurrect_agent forces the row back to 'terminated' + re-raises on a
                # launch failure; termination_source is re-stamped 'launch-confirm', so
                # the corpse stays eligible but the backoff window (already stamped)
                # spaces the retry — no tight loop.
                _log.error("[ops.crash_resurrect] resurrect agent %s failed: %r", tid, exc)
        return True
