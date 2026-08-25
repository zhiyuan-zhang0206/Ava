"""Health reporting for the agent-runner ops control plane.

The wedge threshold follows the rollout family's single no-progress bound with
a five-minute margin. Health degrades only after work has outlived that shared
safety budget and the margin for observation and recovery.
"""

from __future__ import annotations

import time

from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S
from shared.health_schema import DEGRADED, OK, component

_WEDGE_AFTER_S = NO_PROGRESS_TIMEOUT_S + 300.0  # 900s no-progress bound + 5min margin = 1200s


def ops_components(
    update_lock_held_since: float | None,
    active_ops: dict[str, tuple[str, float]],
    *,
    now: float | None = None,
) -> list[dict[str, object]]:
    """Report control-plane progress; only work past the safe bound degrades it."""
    now = time.monotonic() if now is None else now
    if update_lock_held_since is None:
        update_lock = component("update-lock", OK, progress="free")
    else:
        held_s = now - update_lock_held_since
        update_lock = component(
            "update-lock",
            DEGRADED if held_s > _WEDGE_AFTER_S else OK,
            progress=f"held {held_s:.0f}s",
            detail=f"held for {held_s:.0f}s" if held_s > _WEDGE_AFTER_S else None,
        )

    if not active_ops:
        active = component("ops", OK, progress="0 active")
    else:
        detail, started_at = min(active_ops.values(), key=lambda entry: entry[1])
        age_s = now - started_at
        active = component(
            "ops",
            DEGRADED if age_s > _WEDGE_AFTER_S else OK,
            progress=f"{len(active_ops)} active",
            detail=f"{detail} running for {age_s:.0f}s" if age_s > _WEDGE_AFTER_S else None,
        )
    return [component("loop", OK, progress="serving /ops"), update_lock, active]


def saturation(active_ops: dict[str, tuple[str, float]], max_workers: int) -> float:
    """Return the fraction of worker capacity currently occupied by ops."""
    return len(active_ops) / max_workers
