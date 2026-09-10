"""The operator-exclusion view of the `machines` table — which machines are
kept out of the rollout cohort, and since when.

`shared/machines.py` owns the latch writers (`pause` / `resume`, `set_staging`,
`register_self`'s clear of the stop marker) and the rollout-target query
(`list_agent_runners()`). This module answers the companion question the deploy
window asks (issue #2160): which machines' deploy posture can never be recovered
to `idle` by the cluster's own controllers, because an excluded host is
deliberately left alone — so a stale `host_deploy_state` row on one of these is
history, not a competing deployment.

Its own module rather than a function in `shared/machines.py` because that file
sits in the structural 600-800 line transitional zone and this addition would
push it past the hard ceiling (a split, not an exemption).
"""

from __future__ import annotations

from datetime import datetime

import shared.db

__all__ = ["list_excluded_machines"]


def list_excluded_machines() -> list[tuple[str, str, datetime | None]]:
    """`(name, reason, since)` for every machine an operator latch keeps out of
    the rollout cohort — the three exclusions `list_agent_runners()` applies,
    with each one's date.

    `reason` names the latch the row is excluded by, first match wins in the
    order the exits are documented: `"paused"` (the operator pause latch,
    cleared only by `ava cluster resume`), `"staging"` (`ava cluster
    unmark-staging`), `"stopped"` (an `ava stop` announcement, cleared by the
    next completed `ava start`). `since` is that latch's timestamp — the
    `paused_at` / `stopped_at` column; the staging flag has no date column, so
    it reads None. A machine can carry several latches at once (the operator
    paused-then-stopped shape); the order above is what a reader is told.

    Not a rollout-target query — `list_agent_runners()` remains the one the
    fan-out uses (it also applies the capability predicate). This one is read
    by the deploy window to tell an excluded machine's stale posture from a
    competing deployment.
    """
    with shared.db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, "
            "  CASE WHEN paused_at IS NOT NULL THEN 'paused' "
            "       WHEN is_staging THEN 'staging' "
            "       ELSE 'stopped' END, "
            "  CASE WHEN paused_at IS NOT NULL THEN paused_at "
            "       WHEN is_staging THEN NULL "
            "       ELSE stopped_at END "
            "FROM machines "
            "WHERE paused_at IS NOT NULL OR is_staging OR stopped_at IS NOT NULL "
            "ORDER BY name"
        )
        return [(row[0], row[1], row[2]) for row in cur.fetchall()]
