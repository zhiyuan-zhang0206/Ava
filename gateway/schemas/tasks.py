"""task registry.

Split out of the former monolithic ops/schemas.py; FastAPI registers these
unchanged, so the OpenAPI codegen is byte-identical to the wire before.
"""

from pydantic import (
    BaseModel,
    ConfigDict,
)

from shared.priority import Priority


class TaskRow(BaseModel):
    """One full task row for PATCH and GET /api/tasks?fields=full."""

    model_config = ConfigDict(frozen=True)

    id: int
    parent_id: int | None
    title: str
    description: str
    results: str | None
    status: str
    priority: Priority  # P0 (highest) .. P3 (lowest); the board's within-column sort key
    owner: int | None
    owner_label: str | None = None
    created_by: str
    created_at: str  # ISO-8601
    updated_at: str  # ISO-8601
    remind_interval_seconds: int | None = None
    last_reminded_at: str | None = None  # ISO-8601
    reminder_count: int = 0
    ghost: bool = False  # out-of-window ancestor of a kept task (GET /api/tasks with a window) — delivered for tree connectivity and rendered dimmed by the graph


class TaskSummaryRow(BaseModel):
    """One metadata-only task-list row."""

    model_config = ConfigDict(frozen=True)

    id: int
    parent_id: int | None
    title: str
    status: str
    owner: int | None
    owner_label: str | None = None
    created_by: str
    created_at: str  # ISO-8601
    updated_at: str  # ISO-8601
    remind_interval_seconds: int | None = None
    last_reminded_at: str | None = None  # ISO-8601
    reminder_count: int = 0
    priority: Priority  # P0 (highest) .. P3 (lowest); the board's within-column sort key
    ghost: bool = False  # out-of-window ancestor of a kept task (GET /api/tasks with a window) — delivered for tree connectivity and rendered dimmed by the graph


class TaskListResponse(BaseModel):
    """GET /api/tasks response — full rows or compact list summaries."""

    model_config = ConfigDict(frozen=True)

    tasks: list[TaskRow | TaskSummaryRow]


class TaskUpdateRequest(BaseModel):
    """PATCH /api/tasks/{id} — partial update; omitted fields stay unchanged.

    status, title, description, and results are taken when non-null. owner and
    remind_interval_seconds are taken when present (model_fields_set), but an explicit
    null is rejected for both: a task cannot be released (every non-root task
    has an owner) and reminders cannot be disabled (interval capped at 24h).
    parent_id is taken when present (model_fields_set): an explicit null moves
    the task under the system root, an int reparents it (the parent must exist
    and the move must not create a cycle). Owner changes through this endpoint
    do not message the affected agents (the SDK update path does)."""

    status: str | None = None
    title: str | None = None
    description: str | None = None
    results: str | None = None
    remind_interval_seconds: int | None = None
    owner: int | None = None
    priority: Priority | None = None
    parent_id: int | None = None
