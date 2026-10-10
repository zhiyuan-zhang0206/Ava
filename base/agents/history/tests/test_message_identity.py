"""Stored identity provenance survives every history representation and projection."""

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import CheckpointTuple, empty_checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.types import Overwrite
from psycopg import Connection
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint_postgres_walks import (
    HistoryAsyncPostgresSaver as AsyncPostgresSaver,
)
from base.agents.history.checkpoint_postgres_walks import HistoryPostgresSaver as PostgresSaver
from base.agents.history.delta_read_compat import (
    _areconstruct_in_recovery,
    _fold_history,
    _ReadSpan,
    _wrap_history_identity_reads,
    areconstruct_delta_messages,
    reconstruct_delta_messages,
    recovery_reconstruction_scope,
    wrap_saver_reads_with_delta_reconstruction,
)
from base.agents.history.timeline import TimelineItem, build_timeline_items
from base.agents.messages.identity import normalize_stored_message_ids
from base.db import InboundRow


def _tuple(messages: Any) -> CheckpointTuple:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"]["messages"] = messages
    return CheckpointTuple({"configurable": {"thread_id": "identity"}}, checkpoint, {}, None, [])


@pytest.mark.parametrize("snapshot", [False, True])
@pytest.mark.asyncio
async def test_sync_async_reads_mark_plain_and_snapshot_without_store_access(
    snapshot: bool,
) -> None:
    value = [AIMessage(content="legacy"), AIMessage(content="stored", id="persisted")]
    stored = _DeltaSnapshot(value) if snapshot else value
    sync = _tuple(deepcopy(stored))
    async_ = _tuple(deepcopy(stored))
    assert sync.pending_writes is not None and async_.pending_writes is not None
    sync.pending_writes.append(("task", "messages", [AIMessage(content="legacy pending")]))
    async_.pending_writes.append(("task", "messages", [AIMessage(content="legacy pending")]))
    reconstruct_delta_messages(cast(Any, None), sync)
    await areconstruct_delta_messages(cast(Any, None), async_)
    for result in (sync, async_):
        messages = result.checkpoint["channel_values"]["messages"]
        assert messages[0].id is None
        assert messages[0].additional_kwargs == {"ava_ephemeral_message_id": True}
        assert messages[1].additional_kwargs == {}
        assert result.pending_writes is not None
        pending = cast(AIMessage, result.pending_writes[0][2][0])
        assert pending.additional_kwargs.get("ava_ephemeral_message_id") is True


@pytest.mark.parametrize("seed", ["plain", "snapshot", "writes", "overwrite"])
def test_legacy_walk_seed_and_writes_never_upgrade_after_reserialization(seed: str) -> None:
    original = AIMessage(content="legacy")
    entry: dict[str, Any] = {"writes": []}
    if seed == "plain":
        entry["seed"] = [original]
    elif seed == "snapshot":
        entry["seed"] = _DeltaSnapshot([original])
    else:
        value = Overwrite([original]) if seed == "overwrite" else [original]
        entry["writes"] = [(0, "task", value)]
    serde = JsonPlusSerializer()
    first = _fold_history(deepcopy(entry))
    second = _fold_history(deepcopy(entry))
    assert first[0].additional_kwargs["ava_ephemeral_message_id"] is True
    if seed == "writes":
        assert first[0].id and first[0].id != second[0].id
    restored = serde.loads_typed(serde.dumps_typed(first))
    items, _ = build_timeline_items(restored, [])
    assert items[0].source_message_id is None
    assert items[0].source_block_idx is None
    assert "ava_ephemeral_message_id" not in items[0].payload


def test_durable_source_coordinates_survive_compact_renumbering_and_json() -> None:
    msg = AIMessage(
        id="durable",
        content=[{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
    )
    before, _ = build_timeline_items([HumanMessage(content="old head", id="head"), msg], [])
    history, _ = build_timeline_items([msg], [], segment_prefix="s1.boundary")
    current, _ = build_timeline_items([msg], [])
    assert before[1].item_id == "1.0"
    assert history[0].item_id == "s1.boundary.0.0"
    assert current[0].item_id == "0.0"
    for items in (before[1:], history, current):
        assert [(i.source_message_id, i.source_block_idx) for i in items] == [
            ("durable", 0),
            ("durable", 1),
        ]
        assert TimelineItem.model_validate_json(items[0].model_dump_json()) == items[0]
    other, _ = build_timeline_items([AIMessage(id="different", content=msg.content)], [])
    assert other[0].source_message_id != current[0].source_message_id


def test_only_explicit_inbound_id_qualifies_legacy_source() -> None:
    # The same positional anchor remains valid for UI timestamps/correlation.
    anchor = InboundRow(
        id=9,
        kind="chat",
        content="chat",
        source="web",
        status="claimed",
        created_at=datetime.now(UTC),
    )
    legacy = HumanMessage(content="chat", additional_kwargs={"ava_msg_type": "inbound"})
    anchored, _ = build_timeline_items([legacy], [anchor])
    assert anchored[0].inbound_id == 9
    assert anchored[0].source_inbound_id is None
    assert anchored[0].source_block_idx is None
    explicit = HumanMessage(
        content="chat", additional_kwargs={"ava_msg_type": "inbound", "ava_inbound_id": 9}
    )
    qualified, _ = build_timeline_items(normalize_stored_message_ids([explicit]), [])
    assert qualified[0].source_message_id is None
    assert qualified[0].source_inbound_id == 9
    assert qualified[0].source_block_idx == 0


@pytest.mark.asyncio
async def test_real_postgres_plain_checkpoint_sync_async_provenance(
    db_conn: Connection[Any], aops_pool: AsyncConnectionPool
) -> None:
    saver = PostgresSaver(cast(Any, db_conn))
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"]["messages"] = [
        AIMessage(content="legacy"),
        AIMessage(content="new", id="stored"),
    ]
    checkpoint["channel_versions"]["messages"] = "1"
    config = saver.put(
        {"configurable": {"thread_id": "identity-postgres", "checkpoint_ns": ""}},
        checkpoint,
        {"source": "input", "step": -1, "parents": {}},
        {"messages": "1"},
    )
    saver.put_writes(config, [("messages", [AIMessage(content="pending legacy")])], "task")
    db_conn.commit()
    sync = saver.get_tuple(config)
    async_saver = AsyncPostgresSaver(cast(Any, aops_pool))
    async_ = await async_saver.aget_tuple(config)
    assert sync is not None and async_ is not None
    reconstruct_delta_messages(saver, sync)
    await areconstruct_delta_messages(async_saver, async_)
    for result in (sync, async_):
        assert (
            result.checkpoint["channel_values"]["messages"][0].additional_kwargs[
                "ava_ephemeral_message_id"
            ]
            is True
        )
        assert result.pending_writes is not None
        pending = cast(AIMessage, result.pending_writes[0][2][0])
        assert pending.additional_kwargs.get("ava_ephemeral_message_id") is True
    # Normalization is read-only: the stored payload remains legacy raw data.
    raw = saver.get_tuple(config)
    assert raw is not None
    assert raw.checkpoint["channel_values"]["messages"][0].additional_kwargs == {}
    assert raw.checkpoint["channel_values"]["messages"][0].id is None
    _wrap_history_identity_reads(cast(Any, saver))
    iterator = saver.list(config, limit=1)
    listed = next(iterator)
    assert (
        listed.checkpoint["channel_values"]["messages"][0].additional_kwargs[
            "ava_ephemeral_message_id"
        ]
        is True
    )
    cast(Any, iterator).close()
    wrap_saver_reads_with_delta_reconstruction(async_saver)
    async_iterator = async_saver.alist(config, limit=1)
    listed_async = await anext(async_iterator)
    assert (
        listed_async.checkpoint["channel_values"]["messages"][0].additional_kwargs[
            "ava_ephemeral_message_id"
        ]
        is True
    )
    await cast(Any, async_iterator).aclose()


@pytest.mark.asyncio
async def test_history_iterators_preserve_laziness_arguments_and_source_marker() -> None:
    saver = AsyncPostgresSaver(cast(Any, object()))
    events: list[Any] = []

    def tuples(*args: Any, **kwargs: Any):
        events.append((args, kwargs))
        try:
            yield _tuple([AIMessage(content="legacy")])
            events.append("continued")
        finally:
            events.append("closed")

    async def atuples(*args: Any, **kwargs: Any):
        for value in tuples(*args, **kwargs):
            yield value

    saver.list = tuples  # type: ignore[method-assign]
    saver.alist = atuples  # type: ignore[method-assign]
    _wrap_history_identity_reads(saver)
    it = saver.list(None, limit=2)
    assert events == []
    first = next(it)
    assert events == [((None,), {"limit": 2})]
    assert (
        first.checkpoint["channel_values"]["messages"][0].additional_kwargs[
            "ava_ephemeral_message_id"
        ]
        is True
    )
    it.close()
    assert events[-1] == "closed" and "continued" not in events
    events.clear()
    ait = saver.alist(None, limit=2)
    assert events == []
    first_async = await anext(ait)
    assert events == [((None,), {"limit": 2})]
    assert (
        first_async.checkpoint["channel_values"]["messages"][0].additional_kwargs[
            "ava_ephemeral_message_id"
        ]
        is True
    )
    await ait.aclose()
    assert events[-1] == "closed" and "continued" not in events


@pytest.mark.asyncio
async def test_recovery_cache_hit_normalizes_newly_decoded_pending_writes() -> None:
    saver = AsyncPostgresSaver(cast(Any, object()))
    walks: list[int] = []

    async def history(**_kwargs: Any) -> dict[str, Any]:
        walks.append(1)
        return {"messages": {"writes": [(0, "task", [AIMessage(content="legacy")])]}}

    saver.aget_delta_channel_history = history  # type: ignore[method-assign]
    wrap_saver_reads_with_delta_reconstruction(saver)

    def checkpoint() -> CheckpointTuple:
        value = _tuple([])
        del value.checkpoint["channel_values"]["messages"]
        value.checkpoint["channel_versions"]["messages"] = "1"
        value.metadata["counters_since_delta_snapshot"] = {"messages": (1, 0)}
        assert value.pending_writes is not None
        value.pending_writes.append(("pending", "messages", [AIMessage(content="pending legacy")]))
        return value

    first = checkpoint()
    second = checkpoint()
    second.checkpoint["id"] = first.checkpoint["id"]
    with recovery_reconstruction_scope(saver, "identity") as scope:
        assert scope is not None
        await _areconstruct_in_recovery(saver, first, scope.generation, _ReadSpan(), scope)
        await _areconstruct_in_recovery(saver, second, scope.generation, _ReadSpan(), scope)
    assert len(walks) == 1
    assert (
        second.checkpoint["channel_values"]["messages"][0].additional_kwargs[
            "ava_ephemeral_message_id"
        ]
        is True
    )
    assert second.pending_writes is not None
    pending = cast(AIMessage, second.pending_writes[0][2][0])
    assert pending.additional_kwargs.get("ava_ephemeral_message_id") is True


def test_reserved_provenance_is_not_serialized_into_provider_messages() -> None:
    import langchain_anthropic.chat_models as anthropic_models
    import langchain_google_genai.chat_models as google_models
    import langchain_openai.chat_models.base as openai_models

    messages = normalize_stored_message_ids(
        [HumanMessage(content="question"), AIMessage(content="answer")]
    )
    # SDK payload formatters expose bare dict annotations; only the wire
    # payload is relevant here, so narrow the third-party module at this seam.
    anthropic = cast(Any, anthropic_models)._format_messages(messages)
    google = cast(Any, google_models)._parse_chat_history(messages)
    openai = [cast(Any, openai_models)._convert_message_to_dict(message) for message in messages]
    for payload in (anthropic, google, openai):
        assert "ava_ephemeral_message_id" not in repr(payload)
    assert all(m.additional_kwargs["ava_ephemeral_message_id"] for m in messages)
