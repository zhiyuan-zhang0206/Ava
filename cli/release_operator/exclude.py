"""`ava cluster release exclude` — the operator leaves one unit out of a fleet operation.

A recorded operator decision in the fleet journal, taken under the home
operation lock, so it is refused while a coordinator executes (it holds that
lock for its whole run). An included unit may be excluded only while the
operation is held (its journal records an error); a unit the coordinator
already marked `failed` or `unknown` may be excluded at any time. The unit
never returns to the operation: the coordinator orders it (if it hears) to
close and stay closed, and it rejoins only through a converge operation. It
stays stale in the published release state. Excluding an excluded unit again
changes nothing.
"""

from __future__ import annotations

import json
import sys

from cli.release_fleet.policy import UnitKey
from cli.release_fleet.progress import UnitStatus
from cli.release_operator.request import parse_unit
from cli.release_transition.journal import Journal, exclusive


def exclude_unit(journal: Journal, unit: UnitKey, reason: str) -> UnitStatus:
    """Record the exclusion; the unit's resulting status."""
    if not reason.strip():
        raise ValueError("an exclusion records its reason")
    operation = journal.operation
    progress = operation.fleet
    if progress is None:
        raise ValueError("only a fleet operation excludes units")
    if operation.terminal:
        raise ValueError("the operation is complete; a stale unit rejoins through a converge")
    try:
        status = progress.status(unit)
    except KeyError:
        raise ValueError(f"unit {unit.label} takes no part in this operation") from None
    if status.inclusion == "excluded":
        return status
    if status.inclusion == "included" and operation.error is None:
        raise ValueError(
            "an included unit is excluded only while the operation is held for the operator"
        )
    reason_text = f"operator: {reason.strip()}"
    excluded = status.model_copy(update={"inclusion": "excluded", "reason": reason_text})
    units = tuple(excluded if entry.unit == unit else entry for entry in progress.units)
    journal.record_fleet(progress.model_copy(update={"units": units}))
    return excluded


def cmd_release_exclude(*, operation: str, unit: str, reason: str) -> int:
    from shared.paths import ava_home

    try:
        target = parse_unit(unit)
        path = ava_home() / "updates" / operation / "operation.json"
        with exclusive(path) as journal:
            status = exclude_unit(journal, target, reason)
    except (ValueError, OSError, RuntimeError) as exc:
        sys.stderr.write(f"release exclude refused: {exc}\n")
        return 2
    sys.stdout.write(
        json.dumps({"operation": operation, "unit": target.label, "inclusion": status.inclusion})
        + "\n"
    )
    return 0
