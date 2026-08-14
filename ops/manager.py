"""Controller-manager — the tick loop that runs the ops controller list.

The K8s controller-manager analogue: one place that drives every reconciler each
tick, in a fixed order, short-circuiting on the first controller that blocks the
tick. This is the reconcile half of a watchdog round; the service-keepalive
healthchecks run only if no controller blocked (a paused / schema-mismatched /
mid-self-heal host must not have its services revived).

Order is load-bearing. Everything from `pause` down is the watchdog's original gate
ordering; the two reclaimers are the additions, and they go in front of all of it:
1. **updater** — reap a hung `ava-updater` session (agent-runner). Ahead of
   everything, because it is the one controller whose finding is *"the signal the
   controllers behind me defer to is a corpse"*: a hung updater leaves this host
   paused, so a reaper placed after `pause` would be short-circuited away in exactly
   the case it exists for. **Every controller behind it defers to that same session**
   (`current_orchestration`) — `pause`, `pin` and `code` — so for as long as a corpse
   holds the session name, none of them can act. That is the whole reason the reaper's
   position is load-bearing rather than merely tidy, and it grew stronger, not weaker,
   when `pin` and `pause` stopped gating on the update *lease* alone: the lease is
   blind to a watchdog-spawned updater, which is what let the pin controller
   force-checkout underneath a live one (issue #1074). The cost of that coupling is
   that a hung session the reaper cannot kill now stalls three dimensions instead of
   one — it logs ERROR with a kill-session remedy when that happens. It is safe
   first because it never blocks: it deletes a false signal rather than touching a
   service, so the rest of the round runs on a corrected reading of the host.
2. **rollout** — interrupt (then, failing that, kill) an `ava-rollout` orchestration
   that has stopped making progress (gateway). Ahead of `pause` for the same reason
   and a sharper version of it: a rollout pauses this host in Phase A, so this
   controller only ever meets its target on a paused host, and the 2026-08-02 incident
   is 67 minutes of this file logging `round blocked by pause (scope=all)` while the
   hung orchestration held the cluster down. Unlike the reaper above it, its first
   action is a `SIGINT` rather than a kill — the recovery for a failed rollout lives
   *inside* the rollout process — but it is equally safe here because it equally never
   blocks. See `ops.controllers.stalled_rollout`.
3. **pause** — a paused host skips everything else (its services are meant to be
   down; reviving them fights the rollout).
4. **schema** — a schema mismatch blocks before pin (old-code daemons would crash
   on the new schema).
5. **pin** — off-pin self-heal (agent-runner) / warn (gateway).
6. **code** — on-pin but running stale processes: restart (agent-runner). Last
   because it is the narrowest reading of "wrong code here": it only applies once
   the checkout is already right, which is what the pin controller ahead of it
   converges.

A blocking controller reports HOW MUCH it blocks (``BlockScope``), not which
services: a DB-scoped block (schema mismatch, DB unreachable) holds back only the
services that need Postgres, while a host-scoped one (paused, update spawned) holds
back the whole roster. The manager passes that scope up; the watchdog is what
matches it against each service's declared needs.

**Every blocked round says so, and a streak escalates.** A block used to be
invisible: a skipped round and an all-green round were indistinguishable in the log,
so a Windows runner spent 3h07m (2026-07-28 22:04 → 2026-07-29 01:11) never
reconciling its roster once — blocked every minute by ``Schema ahead of code`` while
its ``ava cluster update`` trigger kept failing — and it took a forensic reconstruction
(counting ``_run_check`` lines per hour) to see it, because the absence of log output
WAS the only evidence. Each blocked round now logs which dimension blocked, how wide,
and how many consecutive rounds it has been going on; past
``_BLOCKED_ROUND_ALARM_ROUNDS`` it logs at ERROR, and the round that finally clears a
streak says so too. This is the alarm only, and it stays an alarm: bounding the
underlying *heal* (the ~85 failed ``ava cluster update`` triggers of that window) belongs to
each acting controller's own persistent backoff, and a backed-off round still reports
a block — so fewer attempts never mean a quieter alarm.

Each ``reconcile`` is offloaded to a thread so a blocking DB/HTTP call inside a
controller never freezes the asyncio event loop. The manager records the last
``ReconcileResult`` per dimension so Status (``ops.observe``) can surface which
dimension last reconciled and whether it acted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

from ops.controllers.base import BlockScope, Controller, ReconcileResult
from ops.controllers.code import CodeController
from ops.controllers.pin import PinController
from ops.controllers.schema import SchemaController
from ops.controllers.stalled_rollout import StalledRolloutController
from ops.controllers.stalled_updater import StalledUpdaterController
from ops.controllers.stranded_pause import PauseController
from shared.machine import MachineRole

_log = logging.getLogger("ops.manager")

# Consecutive blocked rounds before the per-round line escalates from WARNING to
# ERROR. 10 rounds ~= 10 minutes at the 60s watchdog interval: "no legitimate rollout
# holds this host longer than this", so a pause or a self-heal that outlives it is the
# abnormal case worth an ERROR. Anything shorter would fire on every ordinary rollout.
# (This used to cite ``STRANDED_PAUSE_TIMEOUT_S`` as the same judgment. It no longer
# is: that constant now bounds only an UNOWNED pause, whose owner is provably gone, so
# it is sized for a spawn gap rather than for a rollout's length.)
_BLOCKED_ROUND_ALARM_ROUNDS = 10


def build_controllers() -> tuple[Controller, ...]:
    """The controller list in reconcile order: updater → rollout → pause → schema → pin
    → code."""
    return (
        StalledUpdaterController(),
        StalledRolloutController(),
        PauseController(),
        SchemaController(),
        PinController(),
        CodeController(),
    )


class ControllerManager:
    """Runs a controller list each tick, short-circuiting on the first blocker."""

    def __init__(self, controllers: Iterable[Controller] | None = None) -> None:
        self._controllers: tuple[Controller, ...] = (
            tuple(controllers) if controllers is not None else build_controllers()
        )
        self._last: dict[str, ReconcileResult] = {}
        # Consecutive rounds this manager has blocked something. Counted across
        # dimensions, not per dimension: the operator question is "how long has this
        # host not been fully reconciling", and a streak that hands off from pause to
        # schema mid-rollout is still one uninterrupted gap.
        self._blocked_streak = 0

    async def reconcile(self, role: MachineRole) -> BlockScope:
        """Run the controllers in order; return the scope the first blocker blocked.

        Short-circuits on the first controller that blocks ANYTHING — the ones after
        it do not run this tick (a paused host never reaches the schema/pin
        reconcilers), matching the watchdog's original early-return gate chain. That
        includes a merely DB-scoped block: the later controllers all read the DB or
        the pin over it, so running them under a dead DB would only add noise, and
        keeping one short-circuit rule keeps the ordering the single thing that
        decides which dimension speaks for a round.

        Returns that controller's ``blocks`` scope, or ``BlockScope.NONE`` when every
        controller passed. Each reconcile is run in a thread so its blocking I/O does
        not stall the event loop.

        Every outcome is logged (``_note_blocked`` / ``_note_unblocked``) so a blocked
        round is never silent — a silent block is what made a 3-hour reconcile gap
        look identical to a healthy host."""
        for controller in self._controllers:
            result = await asyncio.to_thread(controller.reconcile, role)
            self._last[controller.name] = result
            if result.blocks is not BlockScope.NONE:
                self._note_blocked(result)
                return result.blocks
        self._note_unblocked()
        return BlockScope.NONE

    def _note_blocked(self, result: ReconcileResult) -> None:
        """Log this blocked round, escalating once a streak passes the alarm bound.

        Logged every round rather than on change: the operator signal is "this host is
        STILL not reconciling", and a once-only line cannot distinguish a block that
        cleared from one that has been holding for hours — which is exactly the
        ambiguity that forced a forensic log reconstruction.
        """
        self._blocked_streak += 1
        level = (
            logging.ERROR
            if self._blocked_streak >= _BLOCKED_ROUND_ALARM_ROUNDS
            else logging.WARNING
        )
        _log.log(
            level,
            "[ops.manager] round blocked by %s (scope=%s%s), roster NOT fully reconciled "
            "— %d consecutive round(s)",
            result.dimension,
            result.blocks,
            f", {result.detail}" if result.detail else "",
            self._blocked_streak,
        )

    def _note_unblocked(self) -> None:
        """Note the round that clears a streak, so the recovery has a timestamp too —
        without it the log shows a gap starting but never ending."""
        if self._blocked_streak:
            _log.warning(
                "[ops.manager] round no longer blocked after %d consecutive blocked round(s); "
                "the full roster is being reconciled again",
                self._blocked_streak,
            )
            self._blocked_streak = 0

    def last_results(self) -> dict[str, ReconcileResult]:
        """The last ``ReconcileResult`` seen per dimension (for Status). Empty until
        the first tick runs a given controller."""
        return dict(self._last)
