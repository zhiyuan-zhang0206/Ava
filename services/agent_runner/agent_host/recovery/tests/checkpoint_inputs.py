"""Explicit persisted checkpoint rows for claim-window recovery cases."""

import json
from uuid import uuid4

import psycopg
from psycopg.rows import TupleRow

__all__ = ["insert_checkpoint", "insert_write"]


def insert_checkpoint(
    db_conn: psycopg.Connection[TupleRow],
    agent: int,
    checkpoint_id: str,
    ts: str,
    *,
    parent_id: str | None = None,
    messages_version: str | None = None,
) -> None:
    checkpoint: dict[str, object] = {"id": checkpoint_id, "ts": ts}
    if messages_version is not None:
        checkpoint["channel_versions"] = {"messages": messages_version}
    db_conn.execute(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
        "checkpoint, metadata) VALUES (%s, '', %s, %s, %s::jsonb, '{}'::jsonb)",
        (str(agent), checkpoint_id, parent_id, json.dumps(checkpoint)),
    )
    db_conn.commit()


def insert_write(
    db_conn: psycopg.Connection[TupleRow],
    agent: int,
    checkpoint_id: str,
    *,
    idx: int,
    type_tag: str,
    blob: bytes,
) -> None:
    db_conn.execute(
        "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, channel, checkpoint_id, "
        "task_id, idx, type, blob) VALUES (%s, '', 'messages', %s, %s, %s, %s, %s)",
        (str(agent), checkpoint_id, str(uuid4()), idx, type_tag, blob),
    )
    db_conn.commit()
