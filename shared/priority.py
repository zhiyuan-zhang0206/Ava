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


def validate_priority(priority: str) -> None:
    """Reject a priority outside the four rungs (fail fast, don't coerce)."""
    try:
        Priority(priority)
    except ValueError:
        raise ValueError(
            f"priority must be one of {[p.value for p in Priority]}, got {priority!r}"
        ) from None
