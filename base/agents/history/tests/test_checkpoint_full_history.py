"""Lossless transcript reconstruction across compaction boundaries."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import psycopg
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

from base.agents.history.checkpoint import (
    list_compact_boundary_checkpoint_ids,
    load_checkpoint_history_full,
    load_checkpoint_message_count,
    load_checkpoint_messages,
    load_checkpoint_messages_full,
    load_checkpoint_messages_segment,
)
from base.config import settings
from base.db import Database


def _db() -> Database:
    return Database.from_settings()


def test_compact_boundary_lookup_uses_a_closed_autocommit_pool() -> None:
    """Checkpoint cold reads borrow an autocommit connection from a pool of the handle."""
    from base.agents.history import checkpoint

    class _Result:
        def fetchall(self) -> list[tuple[str]]:
            return [("boundary-1",)]

    class _Connection:
        def execute(self, _query: str, _params: tuple[object, ...]) -> _Result:
            return _Result()

        def __enter__(self) -> _Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class _Pool:
        closed = False

        def __init__(self) -> None:
            self.connection_value = _Connection()

        def connection(self) -> _Connection:
            return self.connection_value

        def close(self) -> None:
            self.closed = True

    db_pool = _Pool()
    pool_kwargs: dict[str, object] = {}

    def _pool(**kwargs: object) -> _Pool:
        pool_kwargs.update(kwargs)
        return db_pool

    database = cast(Database, SimpleNamespace(pool=_pool))

    assert checkpoint.list_compact_boundary_checkpoint_ids(database, 7) == ["boundary-1"]
    assert pool_kwargs == {"autocommit": True, "row_factory": None}
    assert db_pool.closed is True


def _put_checkpoint(
    thread_id: str,
    messages: Sequence[BaseMessage],
    *,
    metadata: dict[str, object] | None = None,
    version: str,
) -> str:
    """Write one full messages snapshot and return its checkpoint id."""
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": list(messages)}
    checkpoint["channel_versions"] = {"messages": version, "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.setup()
        saved = saver.put(
            config={"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata=cast(
                CheckpointMetadata,
                metadata or {"source": "input", "step": 1, "parents": {}},
            ),
            new_versions={"messages": version},
        )
    configurable = saved.get("configurable") or {}
    return str(configurable["checkpoint_id"])


def _contents(messages: Sequence[BaseMessage]) -> list[str]:
    return [str(cast(object, cast(Any, message).content)) for message in messages]


def test_load_full_history_returns_empty_without_checkpoint() -> None:
    assert load_checkpoint_messages_full(_db(), 1) == []


def test_load_full_history_matches_latest_without_compaction(db_conn: psycopg.Connection) -> None:
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="task"),
        AIMessage(content="done"),
    ]
    _put_checkpoint("2", messages, version="1")

    assert _contents(load_checkpoint_messages_full(_db(), 2)) == _contents(
        load_checkpoint_messages(_db(), 2)
    )


def test_load_full_history_stitches_compaction_segments(db_conn: psycopg.Connection) -> None:
    first = [
        SystemMessage(content="system"),
        HumanMessage(content="original task"),
        AIMessage(content="original answer"),
    ]
    _put_checkpoint(
        "3",
        first,
        metadata={"source": "input", "step": 1, "parents": {}, "compact_boundary": True},
        version="1",
    )
    latest = [
        SystemMessage(content="system"),
        HumanMessage(content="summary"),
        HumanMessage(content="follow-up"),
        AIMessage(content="follow-up answer"),
    ]
    _put_checkpoint("3", latest, version="2")

    assert _contents(load_checkpoint_messages_full(_db(), 3)) == [
        "system",
        "original task",
        "original answer",
        "summary",
        "follow-up",
        "follow-up answer",
    ]


def test_load_full_history_stitches_multiple_compactions(db_conn: psycopg.Connection) -> None:
    _put_checkpoint(
        "5",
        [SystemMessage(content="system"), HumanMessage(content="first task")],
        metadata={"source": "input", "step": 1, "parents": {}, "compact_boundary": True},
        version="1",
    )
    _put_checkpoint(
        "5",
        [
            SystemMessage(content="system"),
            HumanMessage(content="first summary"),
            HumanMessage(content="second task"),
        ],
        metadata={"source": "input", "step": 2, "parents": {}, "compact_boundary": True},
        version="2",
    )
    _put_checkpoint(
        "5",
        [
            SystemMessage(content="system"),
            HumanMessage(content="second summary"),
            HumanMessage(content="third task"),
        ],
        version="3",
    )

    assert _contents(load_checkpoint_messages_full(_db(), 5)) == [
        "system",
        "first task",
        "first summary",
        "second task",
        "second summary",
        "third task",
    ]


def test_load_full_history_does_not_repeat_latest_boundary(db_conn: psycopg.Connection) -> None:
    messages = [
        SystemMessage(content="system"),
        HumanMessage(content="task"),
        AIMessage(content="done"),
    ]
    _put_checkpoint(
        "6",
        messages,
        metadata={"source": "input", "step": 1, "parents": {}, "compact_boundary": True},
        version="1",
    )

    assert _contents(load_checkpoint_messages_full(_db(), 6)) == _contents(messages)


def test_load_full_history_keeps_session_notes_and_summary(db_conn: psycopg.Connection) -> None:
    _put_checkpoint(
        "4",
        [
            SystemMessage(content="system"),
            HumanMessage(content="original task"),
            AIMessage(content="original answer"),
        ],
        metadata={"source": "input", "step": 1, "parents": {}, "compact_boundary": True},
        version="1",
    )
    _put_checkpoint(
        "4",
        [
            SystemMessage(content="system"),
            HumanMessage(content="[system halt] execute_code completed"),
            HumanMessage(content="compaction summary"),
            HumanMessage(content="follow-up task"),
            AIMessage(content="follow-up answer"),
        ],
        version="2",
    )

    assert _contents(load_checkpoint_messages_full(_db(), 4)) == [
        "system",
        "original task",
        "original answer",
        "[system halt] execute_code completed",
        "compaction summary",
        "follow-up task",
        "follow-up answer",
    ]


def test_load_checkpoint_messages_segment_resolves_exact_boundary_id(
    db_conn: psycopg.Connection,
) -> None:
    older_id = _put_checkpoint(
        "7",
        [SystemMessage(content="system"), HumanMessage(content="older segment")],
        metadata={"source": "input", "step": 1, "parents": {}, "compact_boundary": True},
        version="1",
    )
    newer_id = _put_checkpoint(
        "7",
        [
            SystemMessage(content="system"),
            HumanMessage(content="newer summary"),
            HumanMessage(content="newer segment"),
        ],
        metadata={"source": "input", "step": 2, "parents": {}, "compact_boundary": True},
        version="2",
    )
    current_id = _put_checkpoint(
        "7",
        [SystemMessage(content="system"), HumanMessage(content="current segment")],
        version="3",
    )

    assert list_compact_boundary_checkpoint_ids(_db(), 7) == [newer_id, older_id]
    assert list_compact_boundary_checkpoint_ids(_db(), 7, limit=1) == [newer_id]
    assert _contents(load_checkpoint_messages_segment(_db(), 7, newer_id)) == [
        "newer summary",
        "newer segment",
    ]
    assert _contents(load_checkpoint_messages_segment(_db(), 7, older_id)) == ["older segment"]
    assert load_checkpoint_messages_segment(_db(), 7, current_id) == []
    assert load_checkpoint_messages_segment(_db(), 7, "00000000-0000-0000-0000-000000000000") == []
    assert load_checkpoint_message_count(_db(), 7) == 2


def test_load_checkpoint_messages_segment_returns_empty_without_boundaries(
    db_conn: psycopg.Connection,
) -> None:
    latest_id = _put_checkpoint(
        "8",
        [SystemMessage(content="system"), HumanMessage(content="current segment")],
        version="1",
    )

    assert list_compact_boundary_checkpoint_ids(_db(), 8) == []
    assert load_checkpoint_messages_segment(_db(), 8, latest_id) == []
    assert load_checkpoint_message_count(_db(), 8) == 2
    assert load_checkpoint_message_count(_db(), 9) == 0


def test_history_layout_records_each_segments_own_head_and_body_start(
    db_conn: psycopg.Connection,
) -> None:
    """The stitched list drops the joins' SystemMessages; the layout keeps them."""
    _put_checkpoint(
        "7",
        [SystemMessage(content="system-1"), HumanMessage(content="first task")],
        metadata={"source": "input", "step": 1, "parents": {}, "compact_boundary": True},
        version="1",
    )
    _put_checkpoint(
        "7",
        [
            SystemMessage(content="system-2"),
            HumanMessage(content="first summary"),
            HumanMessage(content="second task"),
        ],
        metadata={"source": "input", "step": 2, "parents": {}, "compact_boundary": True},
        version="2",
    )
    _put_checkpoint(
        "7",
        [HumanMessage(content="second summary"), HumanMessage(content="third task")],
        version="3",
    )

    history = load_checkpoint_history_full(_db(), 7)

    assert _contents(history.messages) == [
        "system-1",
        "first task",
        "first summary",
        "second task",
        "second summary",
        "third task",
    ]
    assert [None if head is None else head.content for head in history.segment_heads] == [
        "system-1",
        "system-2",
        None,
    ]
    assert history.segment_starts == (1, 2, 4)


def test_history_layout_of_a_single_snapshot_and_of_no_checkpoint(
    db_conn: psycopg.Connection,
) -> None:
    assert load_checkpoint_history_full(_db(), 8).segment_starts == ()
    _put_checkpoint(
        "8", [SystemMessage(content="system"), HumanMessage(content="task")], version="1"
    )

    history = load_checkpoint_history_full(_db(), 8)

    assert history.segment_starts == (1,)
    assert [head.content if head else None for head in history.segment_heads] == ["system"]
