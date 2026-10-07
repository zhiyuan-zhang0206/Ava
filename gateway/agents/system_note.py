"""Moved out of the parent module to keep it inside the file-size ceiling."""

from __future__ import annotations

import psycopg
from fastapi import HTTPException
from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.messages.caller_identity import caller_payload
from base.agents.messages.inbound import InboundKind
from base.agents.messages.inbound_provenance import InboundProvenance
from base.db import (
    Database,
    insert_inbound_message,
    insert_inbound_message_in_transaction,
    publish_inbound_wake,
)
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
    *,
    client_message_id: str | None = None,
    resurrect: bool = True,
) -> int:
    """Sync system-note INSERT — via to_thread (pool work off the event loop)."""
    payload: dict[str, object] = {
        "note_tag": note_tag,
        **({"task_id": task_id} if task_id is not None else {}),
    }
    if client_message_id is not None:
        # Delivery policy is part of immutable identity, even though claim
        # does not consume it. Keyless legacy payloads remain unchanged.
        payload["delivery_resurrect"] = resurrect
    prepared_event = None
    with write_transaction(pool) as conn:
        inbound_id = (
            _existing_receipt(
                conn,
                client_message_id,
                agent_id,
                content,
                source,
                payload,
            )
            if client_message_id is not None
            else None
        )
        if inbound_id is None and task_id is not None:
            _validate_task_owner(conn, task_id, agent_id)
        if client_message_id is not None:
            if inbound_id is None:
                try:
                    with conn.transaction(), conn.cursor() as cur:
                        inbound_id, prepared_event = insert_inbound_message_in_transaction(
                            cur,
                            agent_id,
                            content,
                            source,
                            kind=InboundKind.SYSTEM_NOTE.value,
                            payload=payload,
                            provenance=provenance,
                        )
                        cur.execute(
                            "UPDATE inbound_messages SET client_message_id = %s WHERE id = %s",
                            (client_message_id, inbound_id),
                        )
                except UniqueViolation as exc:
                    raise HTTPException(
                        status_code=409, detail="idempotency key already identifies another inbound"
                    ) from exc
        else:
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

    if prepared_event is not None:
        telemetry.emit_prepared(prepared_event)
    publish_inbound_wake(db, bus, agent_id, str(inbound_id))
    return inbound_id


def _existing_receipt(
    conn: psycopg.Connection,
    key: str,
    agent_id: int,
    content: str,
    source: str,
    payload: dict[str, object],
) -> int | None:
    """Serialize same-key inserts and replay before mutable task validation."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
        cur.execute(
            "SELECT id, agent_id, content, kind, source, payload "
            "FROM inbound_messages WHERE client_message_id = %s",
            (key,),
        )
        previous = cur.fetchone()
    if previous is None:
        return None
    if previous[1:] != (
        agent_id,
        content,
        InboundKind.SYSTEM_NOTE.value,
        source,
        caller_payload(source, payload),
    ):
        raise HTTPException(
            status_code=409, detail="idempotency key identifies a different system note"
        )
    return int(previous[0])


def _validate_task_owner(conn: psycopg.Connection, task_id: int, agent_id: int) -> None:
    """Lock current ownership through the note transaction's commit."""
    with conn.cursor() as cur:
        cur.execute("SELECT owner FROM agent_tasks WHERE id = %s FOR UPDATE", (task_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=422, detail=f"task_id {task_id} does not exist")
    if row[0] != agent_id:
        raise HTTPException(
            status_code=422, detail=f"task_id {task_id} is not owned by agent {agent_id}"
        )
