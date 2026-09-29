"""Time-graded severity for expected-but-bounded transition windows.

The 2026-08-04 user ruling treats node outages, rollout windows, watchdog
self-heal, and network recovery as normal transitions before they become
incidents. A live deploy explains the transition for as long as its lease is
live; otherwise the shared defaults leave three minutes for normal recovery,
then grade WARNING until the ten-minute ERROR boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

DEFAULT_WARNING_AFTER_S = 180.0
DEFAULT_ERROR_AFTER_S = 600.0

TransitionSeverity = Literal["warning", "error"]


def transition_severity(
    started_at: datetime,
    now: datetime,
    *,
    deploy_explains: bool = False,
    warning_after_s: float = DEFAULT_WARNING_AFTER_S,
    error_after_s: float = DEFAULT_ERROR_AFTER_S,
) -> TransitionSeverity | None:
    """Grade one transition episode from its true start time.

    A live deploy is the expected window and therefore returns ``None``; its
    lease bounds that explanation. Outside a deploy, elapsed time below the
    warning threshold is the normal-recovery budget, the interval up to the
    error threshold is ``warning``, and later observations are ``error``.
    Naive datetimes are interpreted as UTC so persisted and local callers use
    one timeline.
    """

    if deploy_explains:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    elapsed_s = (now - started_at).total_seconds()
    if elapsed_s < warning_after_s:
        return None
    if elapsed_s < error_after_s:
        return "warning"
    return "error"
