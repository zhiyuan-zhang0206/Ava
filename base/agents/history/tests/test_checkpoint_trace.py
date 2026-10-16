from __future__ import annotations

from typing import cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

from base.agents.history.checkpoint import (
    attach_trace_to_checkpoint,
    load_checkpoint_messages_by_trace,
)
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate


def _db(database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)


def _put_checkpoint(
    thread_id: str,
    messages: list,
    *,
    metadata: dict | None = None,
    version: str = "1",
) -> str:
    ckpt = empty_checkpoint()
    ckpt["channel_values"] = {"messages": messages}
    ckpt["channel_versions"] = {"messages": version, "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
        saved = saver.put(
            config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            checkpoint=ckpt,
            metadata=cast(
                CheckpointMetadata,
                metadata or {"source": "input", "step": 1, "parents": {}},
            ),
            new_versions={"messages": version},
        )
    cfg = saved.get("configurable") or {}
    return str(cfg["checkpoint_id"])


def test_load_by_trace_resolves_checkpoint(db_conn, database_gate: ProcessDbGate) -> None:
    msgs = [HumanMessage(content="system prompt"), AIMessage(content="hi")]
    thread = "42"
    ckpt_id = _put_checkpoint(thread, msgs)

    cid, loaded = load_checkpoint_messages_by_trace(_db(database_gate=database_gate), 42, "a" * 32)
    assert cid is None
    assert loaded == []

    import psycopg

    with psycopg.connect(settings.data_plane.db_url) as conn:
        conn.execute(
            "UPDATE checkpoints SET metadata = metadata || jsonb_build_object('trace_id', %s::text)"
            " WHERE thread_id = %s AND checkpoint_id = %s",
            ("b" * 32, thread, ckpt_id),
        )
    cid, loaded = load_checkpoint_messages_by_trace(_db(database_gate=database_gate), 42, "b" * 32)
    assert cid == ckpt_id
    assert [m.type for m in loaded] == ["human", "ai"]
    assert loaded[0].content == "system prompt"


@pytest.mark.asyncio
async def test_attach_trace_to_checkpoint_stamps_metadata(aops_pool) -> None:
    msgs = [HumanMessage(content="hello")]
    ckpt_id = _put_checkpoint("7", msgs)
    await attach_trace_to_checkpoint(aops_pool, "7", ckpt_id, "c" * 32)

    import psycopg

    with psycopg.connect(settings.data_plane.db_url) as conn:
        row = conn.execute(
            "SELECT metadata->>'trace_id' FROM checkpoints"
            " WHERE thread_id = %s AND checkpoint_id = %s",
            ("7", ckpt_id),
        ).fetchone()
    assert row == ("c" * 32,)

    await attach_trace_to_checkpoint(aops_pool, "7", ckpt_id, "d" * 32)
    with psycopg.connect(settings.data_plane.db_url) as conn:
        row = conn.execute(
            "SELECT metadata->>'trace_id' FROM checkpoints"
            " WHERE thread_id = %s AND checkpoint_id = %s",
            ("7", ckpt_id),
        ).fetchone()
    assert row == ("d" * 32,)


def test_load_by_trace_picks_newest_when_multiple(db_conn, database_gate: ProcessDbGate) -> None:
    thread = "9"
    older = _put_checkpoint(
        thread, [HumanMessage(content="v1")], metadata={"trace_id": "e" * 32}, version="1"
    )
    newer = _put_checkpoint(
        thread, [HumanMessage(content="v2")], metadata={"trace_id": "e" * 32}, version="2"
    )
    cid, loaded = load_checkpoint_messages_by_trace(_db(database_gate=database_gate), 9, "e" * 32)
    assert cid == newer
    assert cid != older
    assert loaded[0].content == "v2"
