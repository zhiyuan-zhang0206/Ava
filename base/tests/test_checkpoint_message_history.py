"""Message resets bound transferred history without changing replay semantics."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base.id import uuid6
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Overwrite

from base.agents.history import checkpoint_postgres_walks as history
from base.agents.history.delta_read_compat import (
    _fold_history,
    wrap_saver_reads_with_delta_reconstruction,
)
from base.config import settings
from base.events.contract import EVENTS, payload_keys


def _checkpoint(saver: PostgresSaver, parent: Any, *, seed: Any = None) -> Any:
    checkpoint_id = str(uuid6(clock_seq=-1))
    return saver.put(
        parent,
        {
            "v": 1,
            "id": checkpoint_id,
            "ts": "",
            "channel_values": {} if seed is None else {"messages": seed},
            "channel_versions": {"messages": checkpoint_id},
            "versions_seen": {},
            "updated_channels": None,
        },
        {
            "source": "loop",
            "step": 1,
            "parents": {},
            "counters_since_delta_snapshot": {"messages": (1, 1)},
        },
        {} if seed is None else {"messages": checkpoint_id},
    )


def _message(name: str, size: int = 0) -> HumanMessage:
    return HumanMessage(id=name, content=name + "x" * size)


@contextmanager
def _record_bodies(
    saver: PostgresSaver, monkeypatch: pytest.MonkeyPatch
) -> Generator[list[list[bytes]]]:
    original = saver._cursor
    bodies: list[list[bytes]] = []

    class Cursor:
        def __init__(self, cursor: Any) -> None:
            self.cursor = cursor

        def execute(self, sql: str, params: Any, *, binary: bool = True) -> Any:
            assert binary
            return self.cursor.execute(sql, params, binary=binary)

        def fetchone(self) -> Any:
            return self.cursor.fetchone()

        def fetchall(self) -> Any:
            rows = self.cursor.fetchall()
            if rows and "blob" in rows[0]:
                assert all(isinstance(row["blob"], bytes) for row in rows)
                bodies.append([row["blob"] for row in rows])
            return rows

    @contextmanager
    def cursor() -> Generator[Cursor]:
        with original() as raw:
            yield Cursor(raw)

    with monkeypatch.context() as patch:
        patch.setattr(saver, "_cursor", cursor)
        yield bodies


@pytest.mark.parametrize(
    "reset_kind", ["remove", "dict_remove", "overwrite", "dict_overwrite", "erased_overwrite"]
)
async def test_suffix_matches_upstream_and_skips_dead_blobs(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, reset_kind: str
) -> None:
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        root = _checkpoint(
            saver,
            {"configurable": {"thread_id": str(uuid4()), "checkpoint_ns": ""}},
            seed=_DeltaSnapshot([_message("seed", 900_000)]),
        )
        old = _checkpoint(saver, root)
        saver.put_writes(old, [("messages", [_message("old", 400_000)])], "old")
        reset = _checkpoint(saver, old)
        values = [_message("summary")]
        reset_values: dict[str, Any] = {
            "remove": [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                _message("discarded-summary"),
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *values,
            ],
            "dict_remove": [{"type": "remove", "id": REMOVE_ALL_MESSAGES, "content": ""}, *values],
            "overwrite": Overwrite(values),
            "dict_overwrite": {"__overwrite__": values},
            "erased_overwrite": {"type": "__overwrite__", "value": values},
        }
        # These three writes share a checkpoint; lexical task/idx order matters.
        saver.put_writes(reset, [("messages", [_message("same-step-old", 400_000)])], "a")
        saver.put_writes(
            reset,
            [("messages", [_message("earlier-index")]), ("messages", reset_values[reset_kind])],
            "b",
        )
        saver.put_writes(reset, [("messages", [_message("same-step-new")])], "c")
        recent = _checkpoint(saver, reset)
        saver.put_writes(recent, [("messages", [_message("recent", 300_000)])], "recent")
        target = _checkpoint(saver, recent)
        saver.put_writes(target, [("messages", [_message("pending")])], "pending")
        # A newer reset on a sibling must never become this target's cutoff.
        sibling = _checkpoint(saver, old)
        saver.put_writes(
            sibling,
            [("messages", [RemoveMessage(id=REMOVE_ALL_MESSAGES), _message("wrong-branch")])],
            "sibling",
        )
        _checkpoint(saver, sibling)

        baseline = cast(Any, PostgresSaver.get_delta_channel_history).__wrapped__(
            saver, config=target, channels=["messages"]
        )
        with _record_bodies(saver, monkeypatch) as bodies:
            actual = saver.get_delta_channel_history(config=target, channels=["messages"])
        assert _fold_history(actual["messages"]) == _fold_history(baseline["messages"])
        assert [m.id for m in _fold_history(actual["messages"])] == [
            "summary",
            "same-step-new",
            "recent",
        ]
        assert "seed" not in actual["messages"]
        assert len(actual["messages"]["writes"]) == 3
        assert sum(len(blob) for batch in bodies for blob in batch) < 310_000
        assert all(
            sum(map(len, batch)) <= history._BODY_BATCH_BYTES or len(batch) == 1 for batch in bodies
        )
        # A historical request before compaction still gets its original seed.
        past = saver.get_delta_channel_history(config=reset, channels=["messages"])
        assert [m.id for m in _fold_history(past["messages"])] == ["seed", "old"]
    async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as async_saver:
        async_result = await async_saver.aget_delta_channel_history(
            config=target, channels=["messages"]
        )
        assert async_result == actual
        raw = await async_saver.aget_tuple(target)
        assert raw is not None and any(
            write[0] == "pending" for write in (raw.pending_writes or [])
        )


def test_empty_and_no_reset_match_upstream(db_conn: psycopg.Connection) -> None:
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        root = _checkpoint(
            saver,
            {"configurable": {"thread_id": str(uuid4()), "checkpoint_ns": ""}},
            seed=[_message("plain-seed")],
        )
        write = _checkpoint(saver, root)
        saver.put_writes(write, [("messages", [_message("tail")])], "tail")
        target = _checkpoint(saver, write)
        for config in [
            root,
            write,
            target,
            {"configurable": {"thread_id": "missing", "checkpoint_id": str(uuid4())}},
        ]:
            expected = cast(Any, PostgresSaver.get_delta_channel_history).__wrapped__(
                saver, config=config, channels=["messages"]
            )
            assert (
                saver.get_delta_channel_history(
                    config=cast(RunnableConfig, config), channels=["messages"]
                )
                == expected
            )


def test_batch_bound_keeps_one_oversized_write_intact() -> None:
    keys = [{"size": size, "idx": i} for i, size in enumerate([100_000, 180_000, 500_000, 1])]
    batches = list(history._body_batches(keys))
    assert [len(batch) for batch in batches] == [1, 1, 1, 1]
    assert [item for batch in batches for item in batch] == keys


def _history_events(records: list[Any], thread: str) -> dict[str, Any]:
    return {
        record["extra"]["event"]: record["extra"]
        for record in records
        if record["extra"].get("thread_id") == thread
    }


@pytest.mark.parametrize(
    "reset,expected_ids,expected_counts",
    [(False, ["seed", "discarded", "summary"], (2, 3, 2)), (True, ["summary"], (2, 2, 1))],
)
def test_transfer_event_and_reconstruction_span_include_overfetch(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[Any],
    reset: bool,
    expected_ids: list[str],
    expected_counts: tuple[int, int, int],
) -> None:
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        thread = str(uuid4())
        root = _checkpoint(
            saver,
            {"configurable": {"thread_id": thread, "checkpoint_ns": ""}},
            seed=_DeltaSnapshot([_message("seed", 900_000)]),
        )
        write = _checkpoint(saver, root)
        value = (
            [RemoveMessage(id=REMOVE_ALL_MESSAGES), _message("summary")]
            if reset
            else [_message("summary")]
        )
        saver.put_writes(
            write, [("messages", [_message("discarded", 10_000)]), ("messages", value)], "reset"
        )
        target = _checkpoint(saver, write)
        wrap_saver_reads_with_delta_reconstruction(cast(AsyncPostgresSaver, saver))
        with _record_bodies(saver, monkeypatch) as bodies:
            restored = saver.get_tuple(target)
        assert restored is not None
        assert [m.id for m in restored.checkpoint["channel_values"]["messages"]] == expected_ids
        events = _history_events(loguru_records, thread)
        suffix, span = events["delta_message_suffix"], events["delta_read_compat"]
        fetched_bytes = sum(len(blob) for batch in bodies for blob in batch)
        assert (span["stage2_blob_bytes"], suffix["fetched_bytes"]) == (
            fetched_bytes,
            sum(map(len, bodies[0])),
        )
        assert (
            suffix["fetched_rows"],
            span["stage2_rows"],
            suffix["retained_writes"],
        ) == expected_counts
        assert (span["stage1_pages"], span["stage1_rows"]) == (1, 3)
        assert span["reset_decode_ms"] > 0
        assert 0 < span["decode_ms"] <= span["history_build_ms"]
        assert suffix["checkpoint_id"] == restored.checkpoint["id"]
        assert set(payload_keys("delta_message_suffix")) <= suffix.keys()


def test_message_suffix_event_contract() -> None:
    spec = EVENTS["delta_message_suffix"]
    assert (spec.category, spec.tier) == ("telemetry", "noise")
    assert set(payload_keys("delta_message_suffix")) == {
        "thread_id",
        "checkpoint_ns",
        "checkpoint_id",
        "candidate_writes",
        "fetched_rows",
        "fetched_bytes",
        "body_batches",
        "retained_writes",
        "reset_found",
    }


def test_reset_decode_failure_reports_its_phase_and_transferred_rows(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, loguru_records: list[Any]
) -> None:
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        thread = str(uuid4())
        root = _checkpoint(saver, {"configurable": {"thread_id": thread, "checkpoint_ns": ""}})
        saver.put_writes(root, [("messages", [_message("unreadable")])], "write")
        target = _checkpoint(saver, root)

        def fail_decode(_value: Any) -> Any:
            raise ValueError("reset probe cannot decode")

        monkeypatch.setattr(saver.serde, "loads_typed", fail_decode)
        wrap_saver_reads_with_delta_reconstruction(cast(AsyncPostgresSaver, saver))
        with pytest.raises(ValueError, match="reset probe cannot decode"):
            saver.get_tuple(target)
        span = _history_events(loguru_records, thread)["delta_read_compat"]
        assert span["outcome"] == "error" and span["failed_phase"] == "reset_decode"
        assert span["stage2_rows"] == 1 and span["stage2_blob_bytes"] > 0
        assert span["reset_decode_ms"] > 0 and span["decode_ms"] == 0
