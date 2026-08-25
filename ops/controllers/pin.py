"""Pin controller — reconcile a host's prod-source HEAD against the cluster pin.

Desired state (from Spec): every host runs the exact commit the cluster is pinned
to (``cluster_target_sha``). Drift is handled capability-split:
- **agent-runner** (the acting half): force-update to the pin
  (``trigger_update(target_sha=pin)``) and skip the tick's healthchecks (the update
  restarts services anyway). Guarded six ways so an auto-update on the live cluster
  cannot thrash: agent-runner only; declines while the last update is mid-flight or
  just failed/recovered (the window where the lease is released but HEAD may still
  be on the failed target, and where a self-heal checkout races the operator's
  retry — the 2026-08-02 mixed-tree incident); declines while a cluster update
  holds the lock; declines while ANY update is in flight on this host (the lock's
  blind spot — a watchdog-spawned updater takes no lease, and being off-pin is that
  updater's own mid-flight state); a persistent per-pin backoff surviving the
  update's restart; and the shared process cooldown. The *lock* guard has one
  exception, and it is issue
  #1020: a *settle hold* naming this host is not a rollout mutating the cluster, it
  is a stated waiting period whose content is "this host has not converged", with
  nobody executing under it. Deferring to it makes the hold and the heal wait on each
  other until the TTL lapses — so that guard asks the shared lease verdict in
  ``narrow`` mode (``LeaseVerdict.waits_for_this_host``). The exception stops there:
  the in-flight-update guard reads a signal a settle hold cannot speak to (a local
  updater holds no lease), so passing one never skips it.
- **gateway** (the warn-only half): a gateway drift needs the full rollout/recovery
  path, not a single-host self-checkout, so it only warns loudly and never gates the
  tick.

Extracted verbatim from ``services.watchdog.daemon`` (``_check_pin_drift`` /
``_warn_gateway_off_pin`` / ``_read_pin_and_head`` + the backoff helpers). The
process cooldown is shared with the schema controller via ``update_trigger``, and
the persistent backoff record with the code controller via ``_heal_record``. The
lease and orchestration reads are the shared copy in
``ops.controllers._deploy_state``, and the guard chain is the table
``_PIN_HEAL_GUARDS`` — one row per predicate, in the order it always ran.

Scope: this dimension is the CHECKOUT only. "HEAD is the pin but the running
processes are not" is a different dimension with a different heal (a restart, not
a checkout) and lives in ``ops.controllers.code``; ``read_pin_and_head`` is shared
with it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from ops.controllers import _heal_record
from ops.controllers._deploy_state import (
    Defer,
    Guard,
    GuardCtx,
    GuardVerdict,
    Proceed,
    ProceedWarn,
    read_lease_state,
    read_orchestration,
    run_guards,
)
from ops.controllers.base import BlockScope, ReconcileResult
from ops.controllers.update_trigger import in_cooldown, trigger_update
from shared.cluster_drift import prod_source_head_sha, prod_source_pin_relation
from shared.cluster_pin import get_cluster_target_sha
from shared.machine import MachineRole

_log = logging.getLogger("ops.controllers.pin")

# Persistent (survives the restart an update forces) per-pin backoff so a self-heal
# that never lands on the pin (e.g. an unfetchable pin sha) cannot become a restart
# loop hammering the cluster. The process cooldown only covers consecutive ticks of
# one process; this covers the spawn → restart → spawn cycle.
_PIN_HEAL_BACKOFF_S = 1800.0

# How long after a failed/recovered/aborted update the pin self-heal stays away.
# The lease guard covers a rollout while it HOLDS the lease; this covers the
# aftermath the lease cannot see: the rollout has ended (lease released) but the
# host may still sit on the failed target, or the operator may be mid-retry. In
# that window a self-heal's checkout is exactly the concurrent git op that
# races the retry and lands a mixed tree (the 2026-08-02 incident: the watchdog
# force-checked-out the pin ~1s after each retry checked out the target, ping-
# ponging HEAD and corrupting the working tree). Fifteen minutes is long enough
# for the retry to finish (or fail and be resumed) while short enough that a
# genuinely drifted host is not left alone indefinitely.
_UPDATE_RECOVERY_WINDOW_S = 900.0


def _pin_heal_attempt_path() -> Path:
    from shared.paths import ava_home

    return ava_home() / "pin_heal_attempt"


def _in_pin_heal_backoff(pin: str) -> bool:
    """True if a self-heal to *this* pin was attempted within the backoff window
    (and, by the caller's contract, the host is still off it — so retrying now would
    just thrash). A different pin, or an expired/absent record, is not in backoff."""
    return _heal_record.in_backoff(_pin_heal_attempt_path(), pin, _PIN_HEAL_BACKOFF_S)


def _record_pin_heal_attempt(pin: str, *, ok: bool = True, error: str | None = None) -> None:
    """Record a self-heal attempt — both successes AND failures, so consecutive
    failures stay visible (an 8-day drift and a 30-minute drift used to look
    identical). Shape + backward compatibility live in ``_heal_record``."""
    _heal_record.record_attempt(_pin_heal_attempt_path(), pin, ok=ok, error=error)


def _clear_pin_heal_attempt() -> None:
    _heal_record.clear(_pin_heal_attempt_path())


def read_pin_and_head() -> tuple[str, str] | None:
    """Return (pin, head) when both are readable, else None (logging the reason).

    Shared by the two pin-drift halves. None means "cannot decide off-pin this tick"
    — no pin set, HEAD unreadable, or a DB hiccup — and the caller no-ops."""
    try:
        pin = get_cluster_target_sha()
    except psycopg.OperationalError:
        return None  # transient DB connectivity (e.g. mid-rollout) — best-effort
    except Exception:
        # A missing pin row / schema / permission error is a real bug, not "no pin".
        # Surface it loudly but do not crash the tick.
        _log.exception("[ops.pin] reading cluster pin failed")
        return None
    if pin is None:
        return None  # no rollout has pinned a commit yet — nothing to compare against
    head = prod_source_head_sha()
    if head is None:
        # Pinned but the prod-source HEAD is unreadable: the node could be off-pin
        # and we cannot tell, so this is itself worth a warning (not silence).
        _log.warning("[ops.pin] cluster pin %s set but prod-source HEAD unreadable", pin)
        return None
    return pin, head


def warn_gateway_off_pin() -> bool:
    """The **gateway** pin-drift half: warn-only.

    The gateway is the pin *writer*; a gateway drift needs the full rollout/recovery
    path, not a single-host ``spawn_update`` that pauses only itself. So this never
    self-heals — it surfaces the drift loudly and always returns False (healthchecks
    still run). The agent-runner half (``check_pin_drift``) owns the acting side."""
    pin_head = read_pin_and_head()
    if pin_head is None:
        return False
    pin, head = pin_head
    if head != pin:
        _log.warning(
            "[ops.pin] gateway off-pin (HEAD %s != pin %s) — needs a rollout, "
            "not a single-host self-heal; not auto-healing",
            head,
            pin,
        )
    return False


def _recent_update_guard(ctx: GuardCtx) -> GuardVerdict:
    """The recent-update guard: defer while an update is mid-flight or has just
    ended badly on this cluster.

    The lease guard below reads the deploy lease, which a rollout holds from
    acquire to release — but the watchdog is NOT the only git mover on this
    host, and the lease is not the only thing that can be mid-move:
    - a rollout's local leg and recovery run inside the lease, yet the recovery
      window between "lease released" and "HEAD back on the pin" is exactly
      when a self-heal's force-checkout races the retry (2026-08-02: HEAD ping-
      ponged target/pin ~1s apart, both checkouts wrote interleaved files, and
      the mixed tree imported as `ModuleNotFoundError`);
    - an update that DIED without finishing leaves the record open and the lease
      stale (ORPHANED) — no lease guard can defer on a lease nobody holds;
    - a recovery that just ended (RECOVERED/ABORTED) may have left HEAD on the
      failed target while an operator is already retrying.

    So this guard defers when the last-update record says an update is RUNNING,
    or ended within the recovery window with a failed/recovered outcome. It
    deliberately lets an INCOMPLETE rollout through: that outcome means the
    gateway landed and the pin advanced, and a host still behind it is
    *supposed* to converge via this very self-heal (issue #1020's settle-hold
    path — the lease guard already handles the hold semantics, and deferring
    here would re-open the mutual wait). CLEAN never matters: after a clean
    rollout HEAD is the pin, so this guard is never reached. An unreadable
    record defers (unreadable evidence is not evidence of absence).
    """
    from shared.last_update import (
        FAILED_OUTCOMES,
        UpdateOutcome,
        read_last_update,
    )

    try:
        rec = read_last_update()
    except Exception:
        _log.warning("[ops.pin] could not read the last-update record; deferring self-heal")
        return Defer
    if rec is None or rec.started_at is None:
        return Proceed  # no update has ever run against this cluster
    if rec.outcome == UpdateOutcome.RUNNING:
        _log.info(
            "[ops.pin] off-pin (HEAD %s != pin %s) but the last update is RUNNING "
            "(started %s) — a rollout's checkout is mid-move and a self-heal "
            "checkout would race it; deferring",
            ctx.head,
            ctx.target,
            rec.started_at,
        )
        return Defer
    if rec.outcome in FAILED_OUTCOMES - {UpdateOutcome.INCOMPLETE} and rec.ended_at is not None:
        age_s = (datetime.now(UTC) - rec.ended_at).total_seconds()
        if age_s <= _UPDATE_RECOVERY_WINDOW_S:
            _log.info(
                "[ops.pin] off-pin (HEAD %s != pin %s) but the last update %s "
                "%.0fs ago (failed at %s) — the host may still be on the failed "
                "target or the operator may be retrying; deferring self-heal",
                ctx.head,
                ctx.target,
                rec.outcome,
                age_s,
                rec.failing_step or "?",
            )
            return Defer
    return Proceed


def _lease_guard(ctx: GuardCtx) -> GuardVerdict:
    """The cluster-update-lease guard, in the pin controller's narrow mode: an
    executing lease defers, a settle hold naming THIS host passes (with the #1020
    warning), a settle hold naming anyone else defers, an unreadable lease defers."""
    verdict = read_lease_state(settle_hold_mode="narrow")
    if verdict.kind == "unreadable":
        _log.warning("[ops.pin] could not read update lock; deferring off-pin self-heal")
        return Defer
    if verdict.kind == "executing" or (
        verdict.kind == "settle_hold" and not verdict.waits_for_this_host
    ):
        _log.info(
            "[ops.pin] off-pin (HEAD %s != pin %s) but a cluster update is in progress "
            "(held by %s); deferring self-heal",
            ctx.head,
            ctx.target,
            verdict.holder,
        )
        return Defer
    if verdict.kind == "settle_hold":
        _log.warning(
            "[ops.pin] off-pin (HEAD %s != pin %s), and the deploy lease is a settle hold "
            "waiting for THIS host (%s) — nothing is executing under it and this checkout is "
            "the convergence it waits for, so the lease guard does not defer; the remaining "
            "guards still apply",
            ctx.head,
            ctx.target,
            verdict.describe,
        )
        return ProceedWarn
    return Proceed


def _orchestration_guard(ctx: GuardCtx) -> GuardVerdict:
    """The local-orchestration guard, and the whole of issue #1074's pin half.

    A watchdog-spawned ``spawn_update`` — the schema controller's heal, this
    controller's own local fallback — never takes a lease, so a host mid-self-update
    reads "no holder" at the lease guard. Off-pin is that update's NORMAL transient:
    it force-checks-out its target and is on the way to ``ava restart``. Forcing the
    checkout back to the pin underneath it does not merely duplicate work, it
    corrupts the run in flight — the updater's trailing ``ava start`` then migrates
    and verifies against a tree it did not check out. That is the 2026-07-31 flap:
    six alternating resets in 111 minutes, each ``origin/main`` leg reverted ~60s
    later and each updater dying on a ``Schema ahead of code`` it had already
    resolved.

    This guard takes NO settle-hold exception, and the asymmetry is the point. The
    two watch different signals: a settle hold is a *lease*, and the updater this
    guard catches takes none at all — so a hold naming this host says nothing about
    whether an updater is running here. Letting the exception carry past this line
    would re-open #1074 in exactly the window #1020 widens. An unreadable answer
    defers the same way (unreadable evidence is not evidence of absence).
    """
    state = read_orchestration()
    if state.kind == "unreadable":
        _log.warning("[ops.pin] could not read the orchestration session; deferring self-heal")
        return Defer
    if state.kind == "in_flight":
        _log.info(
            "[ops.pin] off-pin (HEAD %s != pin %s) but a local %s is in flight — its "
            "checkout is mid-move and forcing this one back would strand it; deferring",
            ctx.head,
            ctx.target,
            state.name,
        )
        return Defer
    return Proceed


def _backoff_guard(ctx: GuardCtx) -> GuardVerdict:
    """A self-heal to a pin that didn't land is not retried within the backoff
    window (survives the update-forced restart)."""
    if _in_pin_heal_backoff(ctx.target):
        _log.warning(
            "[ops.pin] off-pin (HEAD %s != pin %s); a recent self-heal to this pin did "
            "not land — in backoff, not retrying",
            ctx.head,
            ctx.target,
        )
        return Defer
    return Proceed


def _cooldown_guard(ctx: GuardCtx) -> GuardVerdict:
    """The shared process cooldown — the *attempt* is the rate-limited event,
    shared with code/schema so two dimensions drifting in one tick cannot fire two
    updates."""
    if in_cooldown():
        _log.info(
            "[ops.pin] off-pin (HEAD %s != pin %s); self-heal in cooldown, skip",
            ctx.head,
            ctx.target,
        )
        return Defer
    return Proceed


# One row per guard, in the order the chain always ran: recent-update → lease →
# orchestration → backoff → cooldown. `recent_update` is first because it is the
# coarsest signal — any recent update activity (running or just failed/recovered)
# outranks the finer lease/backoff reasoning below it. A settle hold naming this
# host may pass the lease guard, but the rest of the table still applies — the
# #1020 exception is scoped to the lock half and stops there (locked by the
# settle+* tests).
_PIN_HEAL_GUARDS: list[Guard] = [
    Guard("recent_update", _recent_update_guard),
    Guard("lease", _lease_guard),
    Guard("orchestration", _orchestration_guard),
    Guard("backoff", _backoff_guard),
    Guard("cooldown", _cooldown_guard),
]


def check_pin_drift() -> bool:
    """The **agent-runner** pin-drift half: self-heal a runner whose prod-source
    HEAD has drifted off the cluster pin by force-updating it to the pin.

    Returns True **only when it actually spawned** a self-heal (the update restarts
    services, so the tick skips its healthchecks); every other case returns False so
    healthchecks still run. The pin is a floor, not a ceiling: a HEAD at-or-above
    the pin (the pin is an ancestor of HEAD — a failed rollout landed the code but
    never advanced the pin) is converged and is NEVER force-checked-out back to
    the stale pin; only a strictly-behind or diverged HEAD heals (2026-08-25
    downgrade incident). Guards (each matters — this auto-triggers an update on
    the live cluster): agent-runner only (the gateway half warns instead); defers
    while the last update is mid-flight or just failed/recovered (the aftermath
    window where a self-heal checkout races the operator's retry — 2026-08-02);
    declines while a cluster update holds the lock (a not-yet-paused host must not
    race Phase B); declines while ANY local update is in flight (issue #1074); a
    persistent per-pin backoff (a bad pin can't loop across the forced restart);
    and the shared process cooldown."""
    pin_head = read_pin_and_head()
    if pin_head is None:
        return False
    pin, head = pin_head
    if head == pin:
        _clear_pin_heal_attempt()  # back on-pin → reset backoff
        return False
    # The pin is a floor, not a ceiling: a HEAD that already contains the pin
    # is converged, and force-checking it back to the pin is a DOWNGRADE — the
    # exact shape of the 2026-08-25 incident, where a rollout landed the code
    # but died before advancing the pin (stale in-memory credentials after a
    # data-plane rotation), and the self-heal force-checked-out the stale pin
    # underneath the landed gateway, mixing versions while the operator
    # recovered. "ahead" is also what a stray `git pull` produces; the next
    # rollout's force-checkout corrects it, and the warning below keeps it
    # visible. "unknown" (pin commit not present in this checkout) is the one
    # case git cannot decide — never force-checkout blind there either.
    relation = prod_source_pin_relation(pin, head)
    if relation in ("aligned", "ahead"):
        _clear_pin_heal_attempt()
        if relation == "ahead":
            _log.warning(
                "[ops.pin] HEAD %s is AHEAD of pin %s (a failed rollout or a "
                "stray pull) — converged at-or-above the pin; needs a rollout, "
                "not a single-host downgrade; not auto-healing",
                head,
                pin,
            )
        return False
    if relation == "unknown":
        _log.warning(
            "[ops.pin] off-pin (HEAD %s != pin %s) but pin ancestry unknown "
            "(commit not present locally); deferring self-heal",
            head,
            pin,
        )
        return False
    # ── behind / diverged from here: a real checkout heal, not a downgrade ──
    if run_guards(_PIN_HEAL_GUARDS, GuardCtx(head, pin)) is Defer:
        return False
    _log.warning(
        "[ops.pin] off-pin (HEAD %s != pin %s); spawning self-heal update to the pin",
        head,
        pin,
    )
    if trigger_update(target_sha=pin):
        _record_pin_heal_attempt(pin, ok=True)
        return True  # spawned via gateway → update restarts services, skip healthchecks
    # The gateway round-trip failed. Record so operators can see how long
    # this host has been stuck (the system's structural flaw: the more
    # stuck a host is, the less it says).
    _record_pin_heal_attempt(pin, ok=False, error="gateway POST rejected")
    # The gateway round-trip failed. It requires the gateway→runner
    # leg (a machines.gateway_url dial) that a misregistered or unreachable
    # runner cannot fix from here — retrying it forever is a closed loop.
    # This watchdog runs ON the drifting host, so spawn the same updater
    # locally instead.
    from ops.cluster import (
        ClusterUpdateInProgress,
    )
    from ops.cluster import (
        spawn_update as spawn_update_local,
    )

    try:
        session = spawn_update_local(target_sha=pin)
    except ClusterUpdateInProgress:
        _log.warning(
            "[ops.pin] gateway trigger failed; local self-heal blocked by in-flight"
            " update; retry next tick"
        )
        _record_pin_heal_attempt(pin, ok=False, error="local: ClusterUpdateInProgress")
        return False
    except Exception as exc:
        _log.exception("[ops.pin] local self-heal spawn failed; retry next tick")
        # Record the failure like every other exit from this branch. Without it this
        # path arms no backoff at all, so a spawn that fails for a persistent reason
        # retries at the cooldown cadence forever — the unbounded-retry shape PR #879
        # removed from the success-only record, surviving in the one branch it did not
        # reach.
        _record_pin_heal_attempt(pin, ok=False, error=f"local spawn failed: {exc!r}")
        return False
    _log.warning("[ops.pin] gateway trigger failed; spawned LOCAL self-heal: %s", session)
    _record_pin_heal_attempt(pin, ok=True)
    return True  # spawned locally → update restarts services, skip healthchecks


class PinController:
    """Runs the pin-drift reconcile as one manager controller, dispatching on the
    watchdog's ``--role`` capability: the agent-runner acts, the gateway warns."""

    name = "pin"

    def reconcile(self, role: MachineRole) -> ReconcileResult:
        if role == "agent-runner":
            acted = check_pin_drift()
            # A spawned force-update rewrites the checkout and restarts every
            # process here, so it blocks the whole roster (BlockScope.ALL).
            return ReconcileResult(
                dimension=self.name,
                blocks=BlockScope.ALL if acted else BlockScope.NONE,
                acted=acted,
            )
        # gateway (or any non-acting role): warn-only, never gates the tick.
        warn_gateway_off_pin()
        return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)
