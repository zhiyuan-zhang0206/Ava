"""Rendering for the three `agent_tasks` row timestamps — one implementation
for the SDK task registry (issue #181).

The SDK `Task` object is an agent-facing rendered surface, so its three
timestamps (created_at / updated_at / last_reminded_at) must carry the bare
cluster-zone form — the same convention as the `results` notes — never an
explicit offset. The gateway JSON API and the DB keep the explicit-offset
forms; only the SDK object is uniform. Lives in `shared` so any writer that
builds a Task from a row cannot drift from the registry.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from shared.config import format_timestamp

_TS_FIELDS = frozenset({"created_at", "updated_at", "last_reminded_at"})


def render_task_timestamps(row: tuple[Any, ...], col_names: str) -> tuple[Any, ...]:
    """`row` (selected in `col_names` order) with the three timestamp columns
    rendered as bare cluster-zone strings; other columns pass through."""
    out: list[Any] = list(row)
    for i, name in enumerate(col_names.split(", ")):
        value = out[i]
        if isinstance(value, datetime) and name in _TS_FIELDS:
            out[i] = format_timestamp(value)
    return tuple(out)
