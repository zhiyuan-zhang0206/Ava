"""Moved out of the parent module to keep it inside the file-size ceiling."""

from __future__ import annotations

from fastapi import HTTPException
from psycopg_pool import ConnectionPool

from base.agents.messages.inbound import InboundKind
from base.agents.messages.inbound_provenance import InboundProvenance
from base.db import Database, insert_inbound_message
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus


def _system_note_blocking(
    db: Database,
    bus: EventBus,
    pool: ConnectionPool,
    agent_id: int,
    content: str,
    source: str,
    note_tag: str,
    task_id: int | None,
    provenance: InboundProvenance | None = None,
) -> int:
    """Sync system-note INSERT — via to_thread (pool work off the event loop)."""
    with write_transaction(pool) as conn:
        if task_id is not None:
            with conn.cursor() as cur:
                # Keep ownership stable until insert_inbound_message() commits below:
                # a reassignment must not land between attribution validation and
                # queueing the task-tagged note.
                cur.execute("SELECT owner FROM agent_tasks WHERE id = %s FOR UPDATE", (task_id,))
                row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=422, detail=f"task_id {task_id} does not exist")
            if row[0] != agent_id:
                raise HTTPException(
                    status_code=422,
                    detail=f"task_id {task_id} is not owned by agent {agent_id}",
                )
        payload: dict[str, object] = {
            "note_tag": note_tag,
            **({"task_id": task_id} if task_id is not None else {}),
        }
        # No `provenance` keyword at all when there is none (the insert's own default applies).
        extra = {} if provenance is None else {"provenance": provenance}
        return insert_inbound_message(
            conn,
            agent_id,
            content=content,
            source=source,
            kind=InboundKind.SYSTEM_NOTE.value,
            payload=payload,
            database=db,
            bus=bus,
            **extra,
        )
