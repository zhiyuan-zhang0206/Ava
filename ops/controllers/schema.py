"""Schema controller — reconcile the DB's applied migration set against the code's
required set.

Desired state (from Spec): the DB schema matches the migrations the running code
requires. Drift and its heal:
- **code behind DB** (this host missed an update fan-out) → trigger ``ava cluster update``
  (on an agent-runner this takes the git-pull self-update branch that pulls the
  newer code).
- **DB behind code** (the gateway has not migrated yet) → an agent-runner can only
  wait; log + skip, do not spawn (self-update does not apply migrations).
Either drift blocks the healthchecks of the services that USE the DB: reviving one
under a schema mismatch would spawn old-code daemons that crash reading the new
schema (or new daemons crashing on the old schema). Which services those are is not
this controller's business — it reports ``BlockScope.DB_DEPENDENT`` and the watchdog
matches that against what each service declares (``ServiceSpec.requires_db``).

Same bidirectional handling as the watchdog's original ``_schema_reconcile``, same
DB-unreachable safety (log + skip, never assume state or spawn). The shared
update-trigger cooldown lives in ``update_trigger`` so it is the same cooldown the
pin controller honors, and a rejected gateway trigger falls back to the same local
spawn the pin controller uses.

**The heal is rate-limited, the finding is not.** Only the code-behind-DB arm
*retries an action*, and it is the one that hammered: a Windows runner fired ~85
failed ``ava cluster update`` triggers in a 3h07m window because the only limiter in the way
was ``update_trigger``'s 120s process cooldown, which by construction cannot bound
this heal — the heal's whole purpose is to replace the process holding that cooldown,
so a heal that fails and restarts nothing simply re-arms nothing. The persistent
``_heal_record`` backoff (the pin controller's pattern) is what bounds the spawn →
restart → spawn cycle. Every other arm merely *declines to act* and gets no backoff:
one is already inside the cooldown, one is waiting on the gateway to migrate, one is
"the DB is unreachable, state unknown". Rate-limiting a path that spawns nothing
would buy nothing and cost a reader's understanding.

Backing the heal off does NOT quieten the alarm: a backed-off round still reports
``BlockScope.DB_DEPENDENT``, so ``ControllerManager`` still logs the round as blocked
and still escalates the consecutive-round streak to ERROR. Fewer attempts, the same
"this host is not reconciling" signal.

**One code-behind-DB state is not backed off but refused outright: when the cluster
pin is what lacks the migrations** (``pin_is_the_blocker`` — this checkout is behind
the DB *and* this checkout is the pin). The heal moves HEAD forward, the pin
controller moves it back, and nothing advances the pin because advancing it is a step
of a *successful* update: a livelock by construction, and slowing it to one lap per
backoff window only makes it quieter. So the arm logs ERROR with both remedies and
spawns nothing, and the reason rides out on ``ReconcileResult.detail`` so the
manager's escalating blocked-round line names it. That the loop produced no signal a
human would see — the outage was noticed by the frontend being down — is half of what
made it a two-hour outage (issue #1074).
"""

from __future__ import annotations

import logging
from pathlib import Path

import shared.db
from ops.controllers import _heal_record
from ops.controllers._deploy_state import read_lease_state, read_orchestration
from ops.controllers.base import BlockScope, ReconcileResult
from ops.controllers.update_trigger import in_cooldown, trigger_update
from shared.machine import MachineRole
from shared.migrations import (
    CodeBehindSchema,
    MigrationLayoutError,
    SchemaVersionMismatch,
    check_schema_version,
)

_log = logging.getLogger("ops.controllers.schema")

# Persistent (survives the restart an ``ava cluster update`` forces) per-drift backoff for
# the ONE arm that acts. Same window as ``_PIN_HEAL_BACKOFF_S`` / ``_CODE_HEAL_BACKOFF_S``
# and the same judgment: long enough that a heal which cannot land is retried on the
# order of ticks-per-hour rather than ticks-per-minute, short enough that a host does
# not sit behind the schema all day. At the watchdog's 60s round it turns the observed
# ~85 triggers per 3 hours into ~6.
_SCHEMA_HEAL_BACKOFF_S = 1800.0


def _schema_heal_attempt_path() -> Path:
    """This dimension's OWN record, a sibling of ``pin_heal_attempt`` /
    ``code_heal_attempt`` rather than a share of either.

    ``_heal_record`` is shared *machinery*, keyed by the path its caller passes, and
    every controller passes a different one. Sharing the file would make one field
    mean two things: a pin heal's ``ok=False`` would read, to this controller, as "a
    schema heal was attempted and did not land" and suppress a schema heal that might
    have worked — a real possibility, because the two drifts have independent causes
    (an unfetchable pin sha says nothing about whether this checkout carries the DB's
    migrations). The heals do share a rate limit, but it is the process cooldown, and
    it is shared deliberately and by an explicit import.
    """
    from shared.paths import ava_home

    return ava_home() / "schema_heal_attempt"


def _record_schema_heal_attempt(drift: str, *, ok: bool, error: str | None = None) -> None:
    """Record this round's heal attempt — successes AND failures.

    Recording only successes is the exact bug PR #879 fixed in the pin controller: a
    heal that never succeeded never armed its own backoff, so it retried forever. Here
    every round that reaches the spawn point writes exactly one record, at the site
    that knows the final outcome — so ``consecutive_failures`` counts *rounds that
    could not heal*, which is the number an operator wants.
    """
    _heal_record.record_attempt(_schema_heal_attempt_path(), drift, ok=ok, error=error)


def _drift_signature(exc: Exception) -> str:
    """The heal's destination, content-addressed.

    The pin and code controllers key their record on a destination commit. This
    dimension has no destination sha to name — the code must become "a checkout
    carrying the DB's applied migration set", and what identifies that set is the
    check's own report: ``check_schema_version`` builds its message from the SORTED
    applied/required diff, and ``MigrationLayoutError`` from the offending path. So the
    message is stable across processes for one drift and different the moment the drift
    itself changes, which is exactly the equality ``_heal_record.in_backoff`` needs.
    """
    return f"{type(exc).__name__}: {exc}"


def _deploy_already_running() -> tuple[BlockScope, str] | None:
    """`(scope, why)` when a deploy is already running, else None — the guard this
    controller alone was missing.

    The heal here is `ava cluster update`, and every other acting controller declines to spawn
    one while a deploy owns the host. This one did not consult either signal, so it
    could fire into a rollout's Phase B (and, on 2026-07-31, into its own previous
    updater — the spawn half of a flap whose other half was the pin controller pulling
    HEAD back).

    Two signals, deliberately not one. An **executing lease** (`note IS NULL`) is a
    gateway orchestration mutating the cluster; scope `DB_DEPENDENT`, because nothing
    was spawned *here* and the finding is still only "this code is behind the migrated
    DB". A **local orchestration session** is the updater this host spawned for itself,
    which takes no lease at all; scope `ALL`, because that updater is about to replace
    every process here — the same reading `_spawn_update_locally` already gives a
    `ClusterUpdateInProgress`.

    A **settle hold** is deliberately not a deferral: nobody executes under one, and
    the convergence it waits for is exactly what this heal produces. That is issue
    #1020's argument, applied here as `note IS NULL` defers and every settle hold
    passes through — and #1020 has since landed, so the choice is now a standing one
    rather than a merge-order hedge. Pin and code narrowed to
    `DeployLease.awaits(machine_name())`, which defers under a hold naming *another*
    host; this controller deliberately does not follow them there. Their heals move a
    checkout or bounce processes, so licensing one on a host the hold never named is a
    real action taken outside the window's subject. This heal answers "my code is
    behind the DB the whole cluster shares", a condition no other host's settle window
    creates or clears — tightening to `awaits` would only make it wait, under a lease
    nobody is executing under, for a convergence that is not its own. That is the
    mutual wait #1020 exists to remove, and adopting it here would import it.

    Never raises: an unreadable signal defers, because spawning an update on missing
    evidence is the expensive direction.

    Reads go through the shared ``_deploy_state`` layer (the same copy pin and code
    use), in its ``pass`` mode: a settle hold is not a deferral here — this heal
    answers "my code is behind the DB the whole cluster shares", a condition no
    other host's settle window creates or clears, so narrowing to ``awaits`` would
    only make it wait under a lease nobody is executing under for a convergence
    that is not its own (issue #1020's mutual wait, imported). An executing lease
    defers; an unreadable lease or orchestration defers.
    """
    verdict = read_lease_state(settle_hold_mode="pass")
    if verdict.kind == "unreadable":
        _log.warning("[ops.schema] could not read the update lock; deferring the update")
        return BlockScope.DB_DEPENDENT, "the update lock could not be read"
    if verdict.kind == "executing":
        return BlockScope.DB_DEPENDENT, f"a cluster update is in progress ({verdict.holder})"
    state = read_orchestration()
    if state.kind == "unreadable":
        _log.warning("[ops.schema] could not read the orchestration session; deferring the update")
        return BlockScope.DB_DEPENDENT, "the orchestration session could not be read"
    if state.kind == "in_flight":
        return BlockScope.ALL, f"a local {state.name} is in flight"
    return None


def pin_is_the_blocker() -> str | None:
    """The cluster pin, when THE PIN is what lacks the DB's migrations — else None.

    Called only from the code-behind-DB arm, where `check_schema_version` has just
    reported that *this checkout* is missing migrations the DB has applied. If this
    checkout's HEAD is also the cluster pin, the two facts compose into a third: the
    pin names a commit that cannot satisfy the schema. That matters because the heal
    for code-behind-DB is `ava cluster update`, whose whole job is to move HEAD forward — and
    the pin controller's job is to move it back to the pin. Neither yields, nothing
    advances the pin (advancing it is a step of a *successful* update), so the pair
    is a livelock by construction. Six alternating resets in 111 minutes, and the
    outage was noticed by the frontend being down rather than by the loop (#1074).

    HEAD-equals-pin is the whole test, and it is evidence rather than inference: the
    exception in hand already establishes "this checkout lacks them", so the only
    thing left to learn is whether this checkout is the pin. No second git read, no
    fetching the pin's tree.

    Gated on `running_from_prod_source` for the same reason the code controller is: a
    dev worktree's watchdog reads PROD's HEAD while the schema check compares the DB
    against ITS OWN `migrations/`, so the two would be about different trees and
    "HEAD == pin" would say nothing about what the failing checkout carries.
    """
    from ops.controllers.pin import read_pin_and_head
    from shared.cluster_drift import running_from_prod_source

    if not running_from_prod_source():
        return None
    pin_head = read_pin_and_head()
    if pin_head is None:
        return None
    pin, head = pin_head
    return pin if head == pin else None


def schema_reconcile() -> tuple[BlockScope, str | None]:
    """Check DB applied vs local code required; self-heal on mismatch.

    Returns how much of the round the finding holds back — ``NONE`` when the versions
    match, ``DB_DEPENDENT`` when the DB is unusable from here, ``ALL`` when an
    ``ava cluster update`` is now in flight on this host — paired with the one-line reason for
    a non-NONE scope, or None when there is nothing to add.

    The reason is returned rather than only logged because the manager renders
    `ReconcileResult.detail` on its blocked-round line — the line that escalates to
    ERROR once a streak passes `_BLOCKED_ROUND_ALARM_ROUNDS` — and this dimension is
    the one that can block a round for hours. "round blocked by schema" repeated 120
    times is the signal the 2026-07-31 outage did not have; "round blocked by schema,
    the cluster pin 1a90f95 is behind the DB" says which of this dimension's several
    states it is in. (`ControllerManager.last_results` also keeps it per dimension for
    a Status surface to read; nothing reads it yet.)

    Design choices:
    - Reuse ``shared.migrations.check_schema_version`` (bidirectional) rather than
      re-implementing the comparison.
    - ``CodeBehindSchema`` / ``MigrationLayoutError``: local code is stale (version
      gap, or the migrations-dir parser rejecting a file newer code handles) →
      self-heal via ``ava cluster update``. Both share one handler so a layout error can
      never slip into the generic branch and be misread as "DB unreachable". A
      gateway that rejects the trigger falls back to spawning the updater locally
      (``_spawn_update_locally``), the same escape the pin controller has.
      **Scope ``ALL``** once an updater is spawned: that update is about to replace
      every process on this host, so reviving anything into it — DB-dependent or not
      — fights the transition, the same reason the pause controller blocks
      everything. In cooldown, in backoff, or with both trigger paths failed, nothing
      was spawned, so the state is just "this code is behind the migrated DB" →
      ``DB_DEPENDENT``.
      This is the ONLY arm with a backoff, because it is the only one that retries an
      action: it is rate-limited by ``_SCHEMA_HEAL_BACKOFF_S`` keyed on the drift, and
      every round that reaches the spawn records its outcome (both outcomes — a heal
      that only ever fails must still arm its own backoff).
    - ``SchemaVersionMismatch``: DB older than code — the gateway has not migrated;
      an agent-runner can only wait (its ``ava cluster update`` is self-update, no
      migrations), so log warning + skip without spawning. **``DB_DEPENDENT``**: the
      wait is entirely about the DB, and it can last as long as the gateway takes to
      migrate — holding a headed browser down for that window buys nothing.
    - DB unreachable / other exceptions: log + skip the DB-dependent services this
      round; do not assume state, do not spawn. **``DB_DEPENDENT``** for the same
      reason, and this is the arm that matters most: a DB outage is exactly when the
      recovery path for an unrelated crash must keep working. Deliberately does NOT
      touch the heal record in either direction: it cannot arm a backoff (there is no
      drift to key it on — the check never got an answer), and it must not clear one
      (a flapping DB would then reset the backoff every other round, restoring the
      hot loop through the one arm that knows the least).
    """
    try:
        with shared.db.connect(autocommit=True) as conn:
            check_schema_version(conn)
    except (CodeBehindSchema, MigrationLayoutError) as exc:
        # Both signal "local code is behind the migrated DB" and self-heal the same
        # way. MigrationLayoutError must NOT fall through to the generic handler
        # below, which would misreport it as "DB unreachable" and silently never
        # reconcile — the exact failure that once stranded this daemon.
        drift = _drift_signature(exc)
        # The pin is the blocker: escalate instead of spawning an update that cannot
        # survive. Ahead of the backoff because it is not a rate limit — it is a
        # different verdict about the same drift, and the backoff would otherwise let
        # one heal per half hour keep flapping the checkout.
        if (pin := pin_is_the_blocker()) is not None:
            _log.error(
                "[ops.schema] %s; and this checkout IS the cluster pin %s, so the PIN is "
                "what lacks these migrations. `ava cluster update` would move HEAD forward and the "
                "pin controller would force it straight back — nothing advances the pin, "
                "because advancing it is a step of a SUCCESSFUL update. NOT spawning: this "
                "needs a human. Either advance the pin to a commit that carries them "
                "(shared.cluster_pin.advance_pin) or roll the schema back to what the pin "
                "carries (shared.migrations.rollback_to).",
                exc,
                pin,
            )
            return BlockScope.DB_DEPENDENT, f"the cluster pin {pin[:7]} is behind the DB schema"
        # Checked AFTER the pin-is-the-blocker escalation, deliberately. Both return
        # without spawning, so the order only decides which reason the operator gets —
        # and during the 2026-07-31 flap an updater was running for much of the window,
        # so a deferral placed first would have masked the terminal verdict behind a
        # transient one, reproducing the "no signal a human would see" the escalation
        # exists to end. The escalation is also a pure local computation; this costs a
        # DB read.
        #
        # This is the only acting controller that never asked whether a deploy is
        # already running, so it could spawn an `ava cluster update` into the middle of one —
        # the other half of the flap, and a way to collide with a rollout's Phase B on
        # any host. Two signals, the same pair pin and code consult: an EXECUTING lease
        # (`note IS NULL`; a settle hold is a stated waiting period with nobody
        # executing, and issue #1020 is about not deferring to those), and a local
        # orchestration session, which a watchdog-spawned updater has instead of a lease.
        if (deferral := _deploy_already_running()) is not None:
            scope, why = deferral
            _log.info("[ops.schema] %s; %s, deferring the update", exc, why)
            return scope, f"code behind the DB schema; {why}"
        # Persistent backoff first: it is this dimension's own verdict and it survives
        # the restart the heal forces, so it is the stronger statement of the two.
        if _heal_record.in_backoff(_schema_heal_attempt_path(), drift, _SCHEMA_HEAL_BACKOFF_S):
            _log.warning(
                "[ops.schema] %s; a recent ava cluster update did not clear this drift — in "
                "backoff, not retrying (the round is still blocked, and a streak of them "
                "still escalates)",
                exc,
            )
            return BlockScope.DB_DEPENDENT, "code behind the DB schema; heal in backoff"
        if in_cooldown():
            _log.info("[ops.schema] %s; ava cluster update in cooldown, skip", exc)
            return BlockScope.DB_DEPENDENT, "code behind the DB schema; heal in cooldown"
        _log.warning("[ops.schema] %s; spawning ava cluster update", exc)
        if trigger_update():
            _record_schema_heal_attempt(drift, ok=True)
            return BlockScope.ALL, "ava cluster update spawned to catch this code up to the DB"
        if _spawn_update_locally(drift):
            return BlockScope.ALL, "ava cluster update spawned to catch this code up to the DB"
        # Neither the gateway nor the local fallback got an updater running, so no
        # transition owns this host: the finding is back to its narrow self, "this
        # code is behind the migrated DB", and only the DB's users are held back. The
        # failure was already recorded by _spawn_update_locally.
        return BlockScope.DB_DEPENDENT, "code behind the DB schema; no updater could be spawned"
    except SchemaVersionMismatch as exc:
        # Declines to act rather than retrying one: on an agent-runner only the gateway
        # can migrate, so there is no attempt here to rate-limit.
        _log.warning(
            "[ops.schema] %s; awaiting gateway migration, skipping DB-dependent services", exc
        )
        return BlockScope.DB_DEPENDENT, "DB behind this code; awaiting the gateway's migration"
    except Exception as exc:
        # INFO, not ERROR: an unreachable DB is a transient condition (a host in
        # regression, a gateway restart mid-window), and the manager's blocked-round
        # line — which carries this same detail — escalates a streak to ERROR once it
        # outlives _BLOCKED_ROUND_ALARM_ROUNDS. An ERROR here on every round is what
        # turned a 3-minute regression into three spurious errors (2026-08-05
        # a runner); the alarm is the streak, not the single round.
        _log.info(
            "[ops.schema] schema reconcile raised (DB unreachable?), skipping this round's "
            "DB-dependent services: %r",
            exc,
        )
        return BlockScope.DB_DEPENDENT, "the schema check could not answer (DB unreachable?)"
    # Converged: the backoff must not outlive the drift it was arming against, so drop
    # the record here — this is the ONE reading that proves a heal is no longer needed.
    _heal_record.clear(_schema_heal_attempt_path())
    return BlockScope.NONE, None


def _spawn_update_locally(drift: str) -> bool:
    """Fallback for a rejected gateway update trigger — spawn the same updater
    session on this host, and record the round's heal attempt. Returns True when an
    update is in flight on this host afterwards (we spawned it, or one was already
    running), False when nothing is updating — which is what decides whether the
    round's block covers the whole roster or only the DB's users.

    Same shape (and same reason) as the pin controller's fallback: the gateway
    round-trip needs a gateway->runner leg (a ``machines.gateway_url`` dial) that a
    misregistered or unreachable runner cannot repair from here, so retrying it
    every tick is a closed loop. This controller runs ON the host that is behind
    the schema, and ``ops.cluster.spawn_update`` is exactly what the gateway would
    have asked it to run — so run it directly. Best-effort: this is already the
    degraded path.

    The record is written here, not by the caller, because only here is the outcome
    known. Note the two "in flight" readings are NOT the same heal: an updater we
    spawned is ours (``ok=True``), while one that was already running is somebody
    else's and our heal did not fire (``ok=False``) — if that update happens to clear
    the drift, the converged round drops the record anyway, so the false-arming costs
    nothing; if it does not, a bounded pause is precisely the intent.
    """
    from ops.cluster import ClusterUpdateInProgress
    from ops.cluster import spawn_update as spawn_update_local

    try:
        session = spawn_update_local()
    except ClusterUpdateInProgress:
        _log.warning(
            "[ops.schema] gateway trigger failed; local self-heal blocked by an "
            "in-flight update; retry after the backoff"
        )
        _record_schema_heal_attempt(drift, ok=False, error="local: ClusterUpdateInProgress")
        return True  # someone else's updater owns this host — still a full-host transition
    except Exception as exc:
        _log.exception("[ops.schema] gateway trigger failed; local self-heal spawn failed")
        _record_schema_heal_attempt(drift, ok=False, error=f"local spawn failed: {exc!r}")
        return False
    _log.warning("[ops.schema] gateway trigger failed; spawned LOCAL self-heal: %s", session)
    _record_schema_heal_attempt(drift, ok=True)
    return True


class SchemaController:
    """Runs ``schema_reconcile`` as one manager controller. Host-level — both
    capability watchdogs run it (a schema mismatch blocks every service that reads
    the schema)."""

    name = "schema"
    timeout_s: float | None = None

    def reconcile(self, role: MachineRole) -> ReconcileResult:  # noqa: ARG002 — host-level, uniform Controller signature
        blocks, detail = schema_reconcile()
        return ReconcileResult(dimension=self.name, blocks=blocks, detail=detail)
