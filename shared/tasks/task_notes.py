"""The stamped-note line appended to an `agent_tasks.results` column — one
implementation for the two writers.

An agent's task notes are appended by the SDK task registry
(`ava_builtins/plugins/ava_fleet/task_registry.py`) and by the gateway when a
machine pause reassigns a task to the drain owner
(`gateway/routers/_machine_pause.py`). Both build the same line, and both used
to build it by hand from `datetime.now(UTC).astimezone()` — the *writing
machine's* local timezone. A fleet spans machines, so one task's notes arrived
stamped in several different timezones, none of them marked, sitting in one
column of text an agent reads top to bottom.

Kept here so the two writers cannot drift apart, and so the stamp is the same
agent-facing representation as every other timestamp an agent sees:
`shared.config.format_timestamp`, in the cluster timezone the agent is told
about once by its standing context note.
"""

from __future__ import annotations

from datetime import UTC, datetime

from shared.config import format_timestamp


def task_note_line(note: str) -> str:
    """`note` as one stamped, newline-terminated line for `agent_tasks.results`."""
    return f"{format_timestamp(datetime.now(UTC))} {note}\n"
