"""Task status — the shared lifecycle state of a task in the registry.

Single source of truth for the three states: the SDK validation
(ava.tasks create/update/list), the gateway wire schemas and PATCH validation,
the `agent_tasks.status` DB CHECK (tests/test_db_check_enum_sync.py locks
db/schema.sql to this enum), and the OpenAPI enum the frontend types are
generated from all reference this one StrEnum, so none of them can drift.

`"ongoing"` is deliberately absent (user ruling 2026-09-15): it was never a
needed state — the system root task is permanently `in_progress` and immutable,
not a status of its own, and regular tasks use in_progress/done/cancelled only.
"""

from enum import StrEnum


class TaskStatus(StrEnum):
    """Lifecycle state of a task: born `in_progress`; `done`/`cancelled` close it."""

    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"
