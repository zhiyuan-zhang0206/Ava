"""Historical delta walks retain their seed and report interrupted read spans."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import CheckpointMetadata, CheckpointTuple, DeltaChannelHistory
from langgraph.checkpoint.base.id import uuid6
from langgraph.checkpoint.postgres import PostgresSaver as UpstreamPostgresSaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.serde.types import _DeltaSnapshot

from base.agents.history import checkpoint_postgres_walks
from base.agents.history.checkpoint import load_checkpoint_messages_segment
from base.agents.history.checkpoint_postgres_walks import (
    HistoryAsyncPostgresSaver as AsyncPostgresSaver,
)
from base.agents.history.checkpoint_postgres_walks import HistoryPostgresSaver as PostgresSaver
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.config import settings
from base.db import Database


def _db() -> Database:
    return Database.from_settings()


def _append_delta_checkpoint(
    saver: UpstreamPostgresSaver, *, thread: str, step: int, parent: RunnableConfig | None
) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": thread, "checkpoint_ns": ""}
    if parent is not None:
        assert "configurable" in parent
        configurable["checkpoint_id"] = parent["configurable"]["checkpoint_id"]
    config: RunnableConfig = {"configurable": configurable}
    value = (
        [SystemMessage(content="seed", id="seed")]
        if step == 0
        else [HumanMessage(content=f"write-{step}", id=f"write-{step}")]
    )
    metadata: dict[str, Any] = {
        "source": "loop",
        "step": step,
        "writes": {},
        "parents": {},
        "counters_since_delta_snapshot": {"messages": (step, 0)},
    }
    if step == 2:
        metadata["compact_boundary"] = True
    saved = saver.put(
        config,
        {
            "v": 1,
            "id": str(uuid6(clock_seq=-1)),
            "ts": "",
            "channel_values": {"messages": _DeltaSnapshot(value)} if step == 0 else {},
            "channel_versions": {"messages": str(step + 1)},
            "versions_seen": {},
            "updated_channels": None,
        },
        cast(CheckpointMetadata, metadata),
        {"messages": str(step + 1)} if step == 0 else {},
    )
    if step:
        saver.put_writes(saved, [("messages", value)], str(uuid4()))
    return saved


def _write_steps(
    saver: UpstreamPostgresSaver, thread: str, count: int, parent: RunnableConfig | None
) -> list[RunnableConfig]:
    configs: list[RunnableConfig] = []
    for step in range(count):
        parent = _append_delta_checkpoint(saver, thread=thread, step=step, parent=parent)
        configs.append(parent)
    return configs


def _write_numbers(entry: DeltaChannelHistory) -> list[int]:
    return [
        int(cast(str, cast(list[HumanMessage], write[2])[0].content).removeprefix("write-"))
        for write in entry["writes"]
    ]


def _history(saver: UpstreamPostgresSaver, config: RunnableConfig) -> DeltaChannelHistory:
    return saver.get_delta_channel_history(config=config, channels=["messages"])["messages"]


def _assert_walk_has_writes_and_seed(entry: DeltaChannelHistory, expected: list[int]) -> None:
    assert _write_numbers(entry) == expected
    assert "seed" in entry and isinstance(entry["seed"], _DeltaSnapshot)


def _assert_early_checkpoints_walk_back_to_root(
    saver: UpstreamPostgresSaver, first: list[RunnableConfig]
) -> None:
    assert _history(saver, first[0]) == {"writes": []}
    for target, expected in ((1, []), (2, [1]), (3, [1, 2]), (5, [1, 2, 3, 4])):
        _assert_walk_has_writes_and_seed(_history(saver, first[target]), expected)


def _append_newer_checkpoints_across_page_boundary(
    saver: UpstreamPostgresSaver, thread: str, first: list[RunnableConfig], target: RunnableConfig
) -> None:
    parent = first[-1]
    for step in range(6, 1027):
        parent = _append_delta_checkpoint(saver, thread=thread, step=step, parent=parent)
        if step == 1025:  # 1023 newer checkpoints: target is in the first page.
            _assert_walk_has_writes_and_seed(_history(saver, target), [1])
    # 1024 newer checkpoints: target is on page two.
    _assert_walk_has_writes_and_seed(_history(saver, target), [1])


async def test_historical_walk_page_boundary_and_compact_segment(
    db_conn: psycopg.Connection,
) -> None:
    thread = "6380"
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        first = _write_steps(saver, thread, 6, None)
        _assert_early_checkpoints_walk_back_to_root(saver, first)
        target = first[2]
        _append_newer_checkpoints_across_page_boundary(saver, thread, first, target)

    async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as async_saver:
        async_entry = (
            await async_saver.aget_delta_channel_history(config=target, channels=["messages"])
        )["messages"]
    _assert_walk_has_writes_and_seed(async_entry, [1])

    # This is the production boundary shape: a walk checkpoint outside the
    # newest page, read by the gateway as one retained compaction segment.
    assert "configurable" in target
    segment = load_checkpoint_messages_segment(
        _db(), int(thread), cast(str, target["configurable"]["checkpoint_id"])
    )
    assert [message.id for message in segment] == ["write-1"]


def test_adapter_validation_leaves_upstream_unchanged() -> None:
    installed = vars(BasePostgresSaver)["_try_advance_walks"]
    upstream_read = UpstreamPostgresSaver.get_delta_channel_history
    PostgresSaver(cast(Any, object()))
    PostgresSaver(cast(Any, object()))
    assert vars(BasePostgresSaver)["_try_advance_walks"] is installed
    assert UpstreamPostgresSaver.get_delta_channel_history is upstream_read


def test_adapter_rejects_new_dependency_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def changed_version(_distribution_name: str) -> str:
        return "3.1.3"

    monkeypatch.setattr(checkpoint_postgres_walks, "version", changed_version)
    with pytest.raises(RuntimeError, match=r"requires langgraph-checkpoint-postgres==3.1.2"):
        checkpoint_postgres_walks.validate_checkpoint_postgres_api()


def test_adapter_rejects_changed_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    def changed(target_id: str) -> None:
        pass

    changed.__module__ = "langgraph.checkpoint.postgres.base"
    changed.__qualname__ = "BasePostgresSaver._try_advance_walks"
    monkeypatch.setattr(BasePostgresSaver, "_try_advance_walks", staticmethod(changed))
    with pytest.raises(RuntimeError, match="signature or identity changed"):
        checkpoint_postgres_walks.validate_checkpoint_postgres_api()


def test_adapter_rejects_foreign_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BasePostgresSaver, "_try_advance_walks", lambda: None)
    with pytest.raises(TypeError, match="replaced outside Ava"):
        checkpoint_postgres_walks.validate_checkpoint_postgres_api()


# Delta read compatibility failures during the PostgreSQL history walk.


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "thread-a",
            "checkpoint_id": "checkpoint-a",
            "checkpoint_ns": "",
        }
    }


def _failing_saver(*, serde: SerializerProtocol | None = None) -> AsyncPostgresSaver:
    saver = AsyncPostgresSaver(cast(Any, object()), serde=serde)

    async def raw_tuple(config: RunnableConfig) -> CheckpointTuple:
        return CheckpointTuple(
            config,
            cast(
                Any,
                {"id": "checkpoint-a", "channel_values": {}, "channel_versions": {"messages": "v"}},
            ),
            cast(Any, {"counters_since_delta_snapshot": {"messages": 1}}),
            None,
            [],
        )

    saver.aget_tuple = raw_tuple  # type: ignore[method-assign]
    wrap_saver_reads_with_delta_reconstruction(saver)
    return saver


async def test_delta_read_timeout_emits_partial_span(loguru_records: list[Any]) -> None:
    saver = _failing_saver()

    async def timed_out(*, config: RunnableConfig, channels: Sequence[str]) -> Any:
        raise TimeoutError("history timed out")

    saver.aget_delta_channel_history = timed_out  # type: ignore[method-assign]
    with pytest.raises(TimeoutError, match="history timed out"):
        await saver.aget_tuple(_config())
    spans = [
        record["extra"]
        for record in loguru_records
        if record["extra"].get("event") == "delta_read_compat"
    ]
    assert len(spans) == 1
    assert spans[0]["outcome"] == "error"
    assert spans[0]["failed_phase"] == "history_read"
    assert spans[0]["error_type"] == "TimeoutError"
    assert spans[0]["elapsed_ms"] >= spans[0]["history_read_ms"] >= 0
    assert spans[0]["message_count"] is None
    assert spans[0]["cache_hit"] is None


async def test_delta_read_decode_error_emits_partial_span(loguru_records: list[Any]) -> None:
    saver = _failing_saver()

    async def bad_history(*, config: RunnableConfig, channels: Sequence[str]) -> Any:
        return saver._build_delta_channels_writes_history(  # type: ignore[attr-defined]
            channels=["messages"],
            chain_by_ch={"messages": ["checkpoint-a"]},
            seed_ver_by_ch={},
            seed_inline_by_ch={},
            stage2_rows=[
                {
                    "channel": "messages",
                    "_kind": "w",
                    "checkpoint_id": "checkpoint-a",
                    "type": "msgpack",
                    "blob": b"\xc1",
                    "task_id": "task",
                    "idx": 0,
                }
            ],
        )

    saver.aget_delta_channel_history = bad_history  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        await saver.aget_tuple(_config())
    spans = [
        record["extra"]
        for record in loguru_records
        if record["extra"].get("event") == "delta_read_compat"
    ]
    assert len(spans) == 1
    assert spans[0]["outcome"] == "error"
    assert spans[0]["failed_phase"] == "history_read"
    assert spans[0]["error_type"] == "ValueError"
    assert spans[0]["elapsed_ms"] >= spans[0]["history_read_ms"] >= 0
    assert "decode_ms" not in spans[0] and "reset_decode_ms" not in spans[0]
    assert spans[0]["message_count"] is None
    assert spans[0]["cache_hit"] is None


async def test_checkpoint_reads_leave_shared_serializer_unchanged(
    loguru_records: list[Any],
) -> None:
    """Cold readers sharing a serializer must not install process-wide instrumentation."""
    shared_serde = JsonPlusSerializer()
    decode = shared_serde.loads_typed
    first = AsyncPostgresSaver(cast(Any, object()), serde=shared_serde)
    wrap_saver_reads_with_delta_reconstruction(first)
    saver = _failing_saver(serde=shared_serde)
    assert saver.serde is first.serde
    assert shared_serde.loads_typed == decode

    async def bad_history(*, config: RunnableConfig, channels: Sequence[str]) -> Any:
        return saver._build_delta_channels_writes_history(  # type: ignore[attr-defined]
            channels=["messages"],
            chain_by_ch={"messages": ["checkpoint-a"]},
            seed_ver_by_ch={},
            seed_inline_by_ch={},
            stage2_rows=[
                {
                    "channel": "messages",
                    "_kind": "w",
                    "checkpoint_id": "checkpoint-a",
                    "type": "msgpack",
                    "blob": b"\xc1",
                    "task_id": "task",
                    "idx": 0,
                }
            ],
        )

    saver.aget_delta_channel_history = bad_history  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        await saver.aget_tuple(_config())
    spans = [
        record["extra"]
        for record in loguru_records
        if record["extra"].get("event") == "delta_read_compat"
    ]
    assert len(spans) == 1
    assert spans[0]["failed_phase"] == "history_read"
    assert spans[0]["error_type"] == "ValueError"
    assert shared_serde.loads_typed == decode
