"""Priority — the shared stakes axis of tasks and notices.

Single source of truth for the four P0..P3 rungs: the SDK validation
(ava.tasks / ava.ui.notify), the gateway wire schemas (TaskRow / NoticeItem /
the snapshot), and the OpenAPI enum the frontend types are generated from all
reference this one enum, so none of them can drift.
"""

from enum import StrEnum


class Priority(StrEnum):
    """Stakes rung: P0 (highest) through P3 (lowest)."""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


# Default reminder interval (seconds) per priority rung — applied by
# ava.tasks.create() when no explicit remind_interval_seconds is given: the
# higher the stakes, the tighter the leash on an unattended task. An explicit
# interval always wins. Reminders stay capped at 24h and cannot be disabled.
DEFAULT_REMIND_INTERVAL_SECONDS: dict[Priority, int] = {
    Priority.P0: 1800,  # 30 min
    Priority.P1: 3600,  # 1 h
    Priority.P2: 7200,  # 2 h
    Priority.P3: 14400,  # 4 h
}


def validate_priority(priority: str) -> None:
    """Reject a priority outside the four rungs (fail fast, don't coerce)."""
    try:
        Priority(priority)
    except ValueError:
        raise ValueError(
            f"priority must be one of {[p.value for p in Priority]}, got {priority!r}"
        ) from None
