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
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from langgraph.checkpoint.serde.types import _DeltaSnapshot

from shared import checkpoint_postgres_walks
from shared.agents.history.checkpoint import load_checkpoint_messages_segment
from shared.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from shared.config import settings


def _append_delta_checkpoint(
    saver: PostgresSaver, *, thread: str, step: int, parent: RunnableConfig | None
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
    saver: PostgresSaver, thread: str, count: int, parent: RunnableConfig | None
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


def _history(saver: PostgresSaver, config: RunnableConfig) -> DeltaChannelHistory:
    return saver.get_delta_channel_history(config=config, channels=["messages"])["messages"]


async def test_historical_walk_page_boundary_and_compact_segment(
    db_conn: psycopg.Connection,
) -> None:
    thread = "6380"
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        first = _write_steps(saver, thread, 6, None)
        root = _history(saver, first[0])
        assert root == {"writes": []}
        for target, expected in ((1, []), (2, [1]), (3, [1, 2]), (5, [1, 2, 3, 4])):
            entry = _history(saver, first[target])
            assert _write_numbers(entry) == expected
            assert "seed" in entry and isinstance(entry["seed"], _DeltaSnapshot)

        target = first[2]
        parent = first[-1]
        for step in range(6, 1027):
            parent = _append_delta_checkpoint(saver, thread=thread, step=step, parent=parent)
            if step == 1025:  # 1023 newer checkpoints: target is in the first page.
                before = _history(saver, target)
                assert _write_numbers(before) == [1]
                assert "seed" in before and isinstance(before["seed"], _DeltaSnapshot)

        after = _history(saver, target)  # 1024 newer checkpoints: target is on page two.
        assert _write_numbers(after) == [1]
        assert "seed" in after and isinstance(after["seed"], _DeltaSnapshot)

    async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as async_saver:
        async_entry = (
            await async_saver.aget_delta_channel_history(config=target, channels=["messages"])
        )["messages"]
    assert _write_numbers(async_entry) == [1]
    assert "seed" in async_entry and isinstance(async_entry["seed"], _DeltaSnapshot)

    # This is the production boundary shape: a walk checkpoint outside the
    # newest page, read by the gateway as one retained compaction segment.
    assert "configurable" in target
    segment = load_checkpoint_messages_segment(
        int(thread), cast(str, target["configurable"]["checkpoint_id"])
    )
    assert [message.id for message in segment] == ["write-1"]


def test_walk_patch_install_is_idempotent() -> None:
    installed = vars(BasePostgresSaver)["_try_advance_walks"]
    checkpoint_postgres_walks.install_checkpoint_postgres_walk_patch()
    checkpoint_postgres_walks.install_checkpoint_postgres_walk_patch()
    assert vars(BasePostgresSaver)["_try_advance_walks"] is installed


def test_walk_patch_rejects_new_dependency_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def changed_version(_distribution_name: str) -> str:
        return "3.1.3"

    monkeypatch.setattr(checkpoint_postgres_walks, "version", changed_version)
    with pytest.raises(RuntimeError, match=r"requires langgraph-checkpoint-postgres==3.1.2"):
        checkpoint_postgres_walks.install_checkpoint_postgres_walk_patch()


def test_walk_patch_rejects_changed_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    def changed(target_id: str) -> None:
        pass

    changed.__module__ = "langgraph.checkpoint.postgres.base"
    changed.__qualname__ = "BasePostgresSaver._try_advance_walks"
    monkeypatch.setattr(checkpoint_postgres_walks, "_installed_descriptor", [])
    monkeypatch.setattr(BasePostgresSaver, "_try_advance_walks", staticmethod(changed))
    with pytest.raises(RuntimeError, match="signature or identity changed"):
        checkpoint_postgres_walks.install_checkpoint_postgres_walk_patch()


def test_walk_patch_rejects_foreign_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BasePostgresSaver, "_try_advance_walks", staticmethod(lambda: None))
    with pytest.raises(RuntimeError, match="replaced outside Ava"):
        checkpoint_postgres_walks.install_checkpoint_postgres_walk_patch()


# Delta read compatibility failures during the PostgreSQL history walk.


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "thread-a",
            "checkpoint_id": "checkpoint-a",
            "checkpoint_ns": "",
        }
    }


def _failing_saver() -> AsyncPostgresSaver:
    saver = AsyncPostgresSaver(cast(Any, object()))

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
    assert spans[0]["failed_phase"] == "decode"
    assert spans[0]["error_type"] == "ValueError"
    assert spans[0]["stage2_rows"] == 1
    assert spans[0]["stage2_blob_bytes"] == 1
    assert spans[0]["decode_ms"] >= 0
    assert spans[0]["history_build_ms"] >= spans[0]["decode_ms"]
    assert spans[0]["message_count"] is None
    assert spans[0]["cache_hit"] is None
