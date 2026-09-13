"""Evidence and bounded release for an abandoned maintenance hold (task #3270).

The stranded-hold verdict (task #3132) answers "is this hold visibly failed".
This module owns the second half of task #3270's ruling: a hold that nothing is
executing under and whose shepherding process is GONE is not merely declarable
-- when it is still PRE-stop and carries no failed receipts, the unit may be
returned to serving exactly as `ava maintenance resume --cancel` would, after a
bounded observation window. Post-stop holds, failure-carrying holds and holds
whose shepherd cannot be judged stay declaration-only; the window gives a
returning operator the first move.
"""

from __future__ import annotations

import contextlib
import logging

from shared import pause_owner
from shared.hold_driver import DriverLiveness, liveness

_log = logging.getLogger("ops.strand_hold")

# The bounded observation window before the automatic release runs, counted
# from the hold's own `acquired_at` (agent #405's ruling of 2026-09-13: an
# optional 10-30 minute window is the "foolproof" half of the release; 30
# minutes keeps the window comfortably past every legitimate pre-stop step --
# a 300s drain bound, prepare/drain re-runs -- while still bounding the stall).
AUTO_RELEASE_S = 1800.0

# The pre-stop phases whose `resume --cancel` is legal (mirrors
# `cli.commands._maintenance._resume` and `ops.cluster_pause._hold_refusal`).
PRE_STOP_PHASES = frozenset({"preparing", "draining", "drained"})


def hold_snapshot() -> pause_owner.PauseOwnerSnapshot | None:
    """The standing maintenance hold, or None when none holds this unit.

    Raises RuntimeError when the journal itself is unreadable -- the same
    refuse-new-work reading `shared.maintenance.snapshot` takes; the verdict
    turns it into an `unknown` round.
    """
    current = pause_owner.read()
    if current.status == "invalid":
        raise RuntimeError("unreadable pause owner; refusing to judge the hold")
    if current.status != "paused" or current.maintenance is None:
        return None
    return current


def driver_reading(current: pause_owner.PauseOwnerSnapshot) -> DriverLiveness:
    """The recorded shepherd's liveness for a standing hold."""
    return liveness(current.driver)


def maybe_release_abandoned_hold(*, paused_for: float | None) -> None:
    """Run the bounded release for a hold the caller judged `abandoned`.

    Called by the pause controller once the verdict reads `abandoned`
    (ownerless + pre-stop + no failed receipts, with a dead recorded shepherd
    or a failed update leg as the proof). Re-verifies the whole proof under the
    lifecycle lock -- a shepherd that returned, an executing signal that
    appeared, a progressed phase or a new failure all abort silently -- and
    then performs the cancellation through `release_pre_stop_hold`, the twin of
    the operator's `ava maintenance resume --cancel`.

    Before the window passes this is a no-op: the declaration stays visible and
    the operator keeps the first move. A failed release is loud and the hold is
    preserved for the next round.
    """
    from shared.config import settings

    if not settings.gateway.abandoned_hold_auto_release:
        return
    if paused_for is None or paused_for < AUTO_RELEASE_S:
        return

    from shared import ui_update_state

    try:
        with ui_update_state.lifecycle_lock():
            current = hold_snapshot()
            if current is None:
                return
            if driver_reading(current) != "dead":
                return  # a shepherd returned between the verdict and this proof
            from ops.controllers.stranded_pause import _executing_owner

            if _executing_owner() is not None:
                return
            from ops.cluster_pause import release_pre_stop_hold

            release_pre_stop_hold(reason="watchdog: abandoned pre-stop hold auto-release")
        _log.error(
            "[strand-hold] auto-released an abandoned pre-stop hold (%.0fs with no "
            "shepherd and nothing executing)",
            paused_for,
        )
    except Exception as exc:
        _log.error("[strand-hold] automatic release attempt failed; hold retained: %r", exc)
        with contextlib.suppress(Exception):
            from shared.host_deploy_state import mark_stranded_hold

            mark_stranded_hold(f"auto-release failed: {exc!r}"[:200])
