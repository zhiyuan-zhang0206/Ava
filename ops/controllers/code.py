"""Code controller — reconcile the commit this host's PROCESSES hold against its
checkout.

Desired state (from Spec): a host that is on the cluster pin is also *running* it.
The pin controller reconciles the checkout (``HEAD`` vs ``cluster_target_sha``) and
heals by force-checking-out the pin. It has nothing to say about the other half:
``HEAD == pin`` but ``running_sha != HEAD`` — the checkout landed and the processes
were never replaced. That host reads ``pin ✓`` on every dashboard while executing
old code, and nothing ever fixed it.

That is not hypothetical. On 2026-07-28 a rollout's Phase B checked out the target
on wsl and ran ``uv sync``; the restart then correctly refused (its preflight found
the gateway mid-restart) and left the host serving rather than stopping into a state
it could not recover from. Correct — but it left ``HEAD`` on the pin and the
processes on the old commit, which is off *no* controller's map, and a human had to
notice and run ``ava restart``.

The heal is a **restart**, not a checkout: the tree is already right, only the
processes are stale. So this spawns ``ava restart`` through the same detached
updater session the pin heal uses (``ops.cluster.spawn_update(restart_only=True)``),
locally — no gateway round-trip is needed to bounce this host's own services.

Guards, all of which matter because this auto-restarts a live host:
- **agent-runner only** — a gateway restarting itself is the rollout/recovery path's
  job, exactly as in the pin controller.
- **off-pin defers to the pin controller** — when ``HEAD != pin`` the checkout is
  the drift; healing the processes onto a wrong checkout would be noise.
- **only the prod-source process** — ``head_sha`` is read from the installed prod
  checkout while ``running_sha`` is the tree *this process* loaded, so on a dev
  worktree the two legitimately differ and must not be read as drift.
- **no cluster update in flight** — the update lock covers a rollout, and a local
  orchestration session covers a self-update on this host. Mid-rollout, "checkout
  ahead of the processes" IS the normal transient; healing it would fight Phase B.
  **With one exception, which is the whole of issue #1020**: a *settle hold* naming
  this host is not a rollout mutating the cluster, it is a stated waiting period whose
  content is "host X has not converged". Deferring to it makes the two wait on each
  other — on 2026-07-30 a runner sat mixed-code for ~16 minutes and converged only
  when the TTL lapsed, and on a deployment whose settle window is renewed rather than
  left to lapse it would not have converged at all. So the guard consults the shared
  lease verdict in ``narrow`` mode (``LeaseVerdict.waits_for_this_host``): an
  executing lease still defers, a settle hold naming this host proceeds. The hold
  itself is untouched — ``ops.deploy_window`` ends it when it observes the
  convergence this restart produces. The exception is scoped to the *lock* half of
  this bullet and stops there: a local orchestration session is a live updater that
  took no lease at all, so a settle hold says nothing about it and passing one never
  skips that check.
- **persistent per-target backoff + the shared process cooldown** — a restart that
  keeps declining (the preflight refusing every time) must not become a restart loop
  across the very restarts it spawns.

The lease and orchestration reads are the shared copy in
``ops.controllers._deploy_state`` (one implementation for pin/code/schema, with
the settle-hold difference as a parameter); the guards themselves are a table —
``_CODE_HEAL_GUARDS`` — one row per predicate, in the order they always ran. The
detection (``_stale_process_state``) and the action (``_spawn_stale_restart``) sit
beside it so a strategy change is editing a table row, not re-reading eleven
branches.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

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
from ops.controllers.pin import read_pin_and_head
from ops.controllers.update_trigger import arm_cooldown, in_cooldown
from shared import process_sha
from shared.cluster_drift import running_from_prod_source
from shared.machine import MachineRole

_log = logging.getLogger("ops.controllers.code")

# Same window as the pin heal: long enough that a restart which cannot land (a
# preflight that keeps refusing) is retried on the order of ticks-per-hour rather
# than every tick, short enough that a host does not sit on stale code all day.
_CODE_HEAL_BACKOFF_S = 1800.0


def _code_heal_attempt_path() -> Path:
    from shared.paths import ava_home

    return ava_home() / "code_heal_attempt"


def _stale_process_state() -> tuple[str, str] | None:
    """Detection: ``(head, running)`` when this host is on-pin but running stale
    code — the actionable drift — else None.

    None covers every "cannot act / nothing to act on" detection branch: no pin,
    HEAD unreadable, off-pin (the checkout is the drift and the pin controller
    owns it), an unknown running sha, and a dev-worktree layout difference. A
    converged host also clears the backoff here — the record must not outlive the
    drift it was arming against.
    """
    pin_head = read_pin_and_head()
    if pin_head is None:
        return None
    pin, head = pin_head
    if head != pin:
        return None  # off-pin: the checkout is the drift, and the pin controller owns it
    running = process_sha.get()
    if running is None or running == head:
        # Either this process never froze a commit (nothing to compare) or it is
        # running exactly what is checked out — converged, so the backoff must not
        # outlive the drift.
        _heal_record.clear(_code_heal_attempt_path())
        return None
    if not running_from_prod_source():
        # A dev-worktree watchdog reads PROD's HEAD but its own tree's commit; the
        # difference is the layout, not drift.
        return None
    return head, running


def _lease_guard(ctx: GuardCtx) -> GuardVerdict:
    """The cluster-update-lease guard, in the code controller's narrow mode: an
    executing lease defers, a settle hold naming THIS host passes (with the #1020
    warning), a settle hold naming anyone else defers, an unreadable lease defers."""
    verdict = read_lease_state(settle_hold_mode="narrow")
    if verdict.kind == "unreadable":
        _log.warning("[ops.code] could not read update lock; deferring stale-code restart")
        return Defer
    if verdict.kind == "executing" or (
        verdict.kind == "settle_hold" and not verdict.waits_for_this_host
    ):
        _log.info(
            "[ops.code] on-pin %s but running %s; a cluster update is in progress "
            "(held by %s) — that update is what replaces these processes, deferring",
            ctx.head,
            ctx.target,
            verdict.holder,
        )
        return Defer
    if verdict.kind == "settle_hold":
        _log.warning(
            "[ops.code] on-pin %s but running %s, and the deploy lease is a settle hold "
            "waiting for THIS host (%s) — nothing is executing under it and this restart is "
            "the convergence it waits for, so the lease guard does not defer; the remaining "
            "guards still apply",
            ctx.head,
            ctx.target,
            verdict.describe,
        )
        return ProceedWarn
    return Proceed


def _orchestration_guard(ctx: GuardCtx) -> GuardVerdict:
    """The local-orchestration guard: a live updater on this host is still the
    deploy that is going to replace these processes, and an unreadable answer
    defers (the conservative direction, adopted from the pin controller — this
    read used to let exceptions propagate)."""
    state = read_orchestration()
    if state.kind == "unreadable":
        _log.warning(
            "[ops.code] could not read the orchestration session; deferring stale-code restart"
        )
        return Defer
    if state.kind == "in_flight":
        _log.info(
            "[ops.code] on-pin %s but running %s; a local %s is in flight — "
            "stale processes are its normal transient, deferring",
            ctx.head,
            ctx.target,
            state.name,
        )
        return Defer
    return Proceed


def _backoff_guard(ctx: GuardCtx) -> GuardVerdict:
    """A restart that keeps declining must not become a restart loop across the
    very restarts it spawns — the record is on disk precisely to survive them."""
    if _heal_record.in_backoff(_code_heal_attempt_path(), ctx.head, _CODE_HEAL_BACKOFF_S):
        _log.warning(
            "[ops.code] on-pin %s but still running %s; a recent restart did not land "
            "— in backoff, not retrying",
            ctx.head,
            ctx.target,
        )
        return Defer
    return Proceed


def _cooldown_guard(ctx: GuardCtx) -> GuardVerdict:
    """The shared process cooldown — the *attempt* is the rate-limited event,
    shared with pin/schema so two dimensions drifting in one tick cannot fire two
    updates."""
    if in_cooldown():
        _log.info(
            "[ops.code] on-pin %s but running %s; restart in cooldown, skip", ctx.head, ctx.target
        )
        return Defer
    return Proceed


# One row per guard, in the order the chain always ran: lease → orchestration →
# backoff → cooldown. A settle hold naming this host may pass the lease guard,
# but the rest of the table still applies — the #1020 exception is scoped to the
# lock half and stops there (locked by the settle+* tests).
_CODE_HEAL_GUARDS: list[Guard] = [
    Guard("lease", _lease_guard),
    Guard("orchestration", _orchestration_guard),
    Guard("backoff", _backoff_guard),
    Guard("cooldown", _cooldown_guard),
]


def _spawn_stale_restart(head: str, running: str) -> bool:
    """Action: arm the cooldown, spawn the restart, record the attempt.

    Returns True only when a restart was actually spawned. ``arm_cooldown``
    precedes the spawn because the *attempt* is the rate-limited event, shared
    with pin/schema; the persistent record covers the spawn → restart → spawn
    cycle the process cooldown cannot.
    """
    from ops.cluster import ClusterUpdateInProgress, spawn_update

    _log.warning(
        "[ops.code] on-pin %s but the running processes hold %s — the checkout landed and "
        "was never restarted; spawning a restart",
        head,
        running,
    )
    arm_cooldown()  # the *attempt* is the rate-limited event, shared with pin/schema
    try:
        session = spawn_update(restart_only=True)
    except ClusterUpdateInProgress:
        # Raced an orchestration that started between the check above and here.
        _log.warning(
            "[ops.code] stale-code restart blocked by an in-flight update; retry next tick"
        )
        return False
    except Exception as exc:
        _log.exception("[ops.code] stale-code restart spawn failed; retry next tick")
        _heal_record.record_attempt(
            _code_heal_attempt_path(), head, ok=False, error=f"spawn failed: {exc!r}"
        )
        return False
    _log.warning("[ops.code] spawned stale-code restart: %s", session)
    _heal_record.record_attempt(_code_heal_attempt_path(), head, ok=True)
    # The stale process this controller measures is itself: `running` is this
    # watchdog's own frozen sha (shared.process_sha), and the restart-only
    # bounce restarts every other service but never the supervisor, so a
    # stale watchdog would otherwise survive every heal and re-arm the loop
    # (Task #1060). Exit and let the restarter — fresh code once the bounce
    # brings it back — respawn this watchdog aligned. The updater session is
    # independent of this process, so the bounce proceeds either way.
    _log.warning(
        "[ops.code] stale process is this watchdog itself; exiting so the "
        "restarter respawns it on %s",
        head,
    )
    os._exit(0)
    return True


def check_code_drift() -> bool:
    """Self-heal a host whose checkout is on the cluster pin but whose processes are
    not, by restarting its services.

    Returns True **only when it actually spawned** a restart (services are coming
    down, so the tick skips its healthchecks); every other case returns False so
    healthchecks still run. See the module docstring for why each guard is here.
    """
    stale = _stale_process_state()
    if stale is None:
        return False
    head, running = stale
    if run_guards(_CODE_HEAL_GUARDS, GuardCtx(head, running)) is Defer:
        return False
    return _spawn_stale_restart(head, running)


class CodeController:
    """Runs the running-code reconcile as one manager controller. Agent-runner only:
    a gateway whose processes are stale needs the rollout/recovery path, not a
    self-restart, and says so through the pin controller's warn-only half."""

    name = "code"
    timeout_s: float | None = None

    def reconcile(self, role: MachineRole) -> ReconcileResult:
        if role != "agent-runner":
            return ReconcileResult(dimension=self.name, blocks=BlockScope.NONE)
        acted = check_code_drift()
        # A spawned restart replaces every process on this host, so it blocks the
        # whole roster (BlockScope.ALL) — not just the DB's users.
        return ReconcileResult(
            dimension=self.name,
            blocks=BlockScope.ALL if acted else BlockScope.NONE,
            acted=acted,
        )
