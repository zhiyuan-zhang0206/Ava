"""Trace↔checkpoint correlation (trace v2, task #792 group C).

`attach_trace_to_checkpoint` stamps a turn's OTel trace_id into the committed
checkpoint's metadata; `load_checkpoint_messages_by_trace` resolves content
back from a trace id. These are the two halves of the on-demand content path:
spans are metadata-only, the checkpoints table holds the full turn messages.
"""

from __future__ import annotations

from typing import cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

from shared.checkpoint import (
    attach_trace_to_checkpoint,
    load_checkpoint_messages_by_trace,
)
from shared.config import settings


def _put_checkpoint(
    thread_id: str,
    messages: list,
    *,
    metadata: dict | None = None,
    version: str = "1",
) -> str:
    """Write one checkpoint and return its checkpoint_id.

    `version` must be unique per messages payload — the blob store keys on
    (channel, version), so two checkpoints sharing a version share a blob
    (langgraph's real runtime monotonically increments channel versions).
    """
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


def test_load_by_trace_resolves_checkpoint(db_conn) -> None:
    """A checkpoint stamped with trace_id resolves to its full messages
    (system prompt included — the channel is the whole conversation)."""
    msgs = [HumanMessage(content="system prompt"), AIMessage(content="hi")]
    thread = "42"
    ckpt_id = _put_checkpoint(thread, msgs)

    # No stamp yet -> pruned shape.
    cid, loaded = load_checkpoint_messages_by_trace(42, "a" * 32)
    assert cid is None
    assert loaded == []

    # Stamp + resolve.
    # sync call path: stamp via plain SQL (attach itself is exercised in the
    # async test below with the prod-shaped async pool).
    # the async pool fixture in the async test below instead. Here, stamp via
    # plain SQL to keep this test sync.
    import psycopg

    with psycopg.connect(settings.data_plane.db_url) as conn:
        conn.execute(
            "UPDATE checkpoints SET metadata = metadata || jsonb_build_object('trace_id', %s::text)"
            " WHERE thread_id = %s AND checkpoint_id = %s",
            ("b" * 32, thread, ckpt_id),
        )
    cid, loaded = load_checkpoint_messages_by_trace(42, "b" * 32)
    assert cid == ckpt_id
    assert [m.type for m in loaded] == ["human", "ai"]
    assert loaded[0].content == "system prompt"  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.asyncio
async def test_attach_trace_to_checkpoint_stamps_metadata(aops_pool) -> None:
    """attach_trace_to_checkpoint merges trace_id into the checkpoint's
    metadata jsonb (idempotent — re-stamping replaces the same key)."""
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

    # Idempotent overwrite.
    await attach_trace_to_checkpoint(aops_pool, "7", ckpt_id, "d" * 32)
    with psycopg.connect(settings.data_plane.db_url) as conn:
        row = conn.execute(
            "SELECT metadata->>'trace_id' FROM checkpoints"
            " WHERE thread_id = %s AND checkpoint_id = %s",
            ("7", ckpt_id),
        ).fetchone()
    assert row == ("d" * 32,)


def test_load_by_trace_picks_newest_when_multiple(db_conn) -> None:
    """Multiple checkpoints of one thread carrying the same trace_id resolve to
    the newest (checkpoint_id DESC) — a re-run inside one trace re-commits."""
    thread = "9"
    older = _put_checkpoint(
        thread, [HumanMessage(content="v1")], metadata={"trace_id": "e" * 32}, version="1"
    )
    newer = _put_checkpoint(
        thread, [HumanMessage(content="v2")], metadata={"trace_id": "e" * 32}, version="2"
    )
    cid, loaded = load_checkpoint_messages_by_trace(9, "e" * 32)
    assert cid == newer
    assert cid != older
    assert loaded[0].content == "v2"  # pyright: ignore[reportUnknownMemberType]
