"""Timeline cases: the cold read fetches only the chat anchors its render consumes.

`list_chat_anchors` returns the oldest bounded prefix plus every referenced
row, so an agent past the bound keeps its newest referenced anchors. The
endpoint skips the inbound read for an all-modern checkpoint and reads only
referenced rows when no legacy inbound aligns positionally.
"""

from __future__ import annotations

from datetime import datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from base.db import ChatAnchor, Database, create_agent, insert_inbound_message, list_chat_anchors
from base.events.live.bus import EventBus
from gateway.agents.history import timeline as gateway_timeline
from gateway.agents.history.tests.test_timeline import CompactHistoryCases
from gateway.agents.history.tests.test_timeline import test_client as test_client


def _chat(db_conn: psycopg.Connection, agent_id: int, database: Database, bus: EventBus) -> int:
    return insert_inbound_message(
        db_conn, agent_id, "chat", source="user", kind="chat", bus=bus, database=database
    )


def _created_at(db_conn: psycopg.Connection, inbound_id: int) -> datetime:
    with db_conn.cursor() as cur:
        cur.execute("SELECT created_at FROM inbound_messages WHERE id = %s", (inbound_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _modern_inbound(inbound_id: int, *, created_at: str | None) -> HumanMessage:
    kwargs: dict[str, object] = {
        "ava_msg_type": "inbound",
        "ava_source": "user",
        "ava_inbound_id": inbound_id,
    }
    if created_at is not None:
        kwargs["ava_created_at"] = created_at
    return HumanMessage(content=f"inbound {inbound_id}", additional_kwargs=kwargs)


def _put(agent_id: int, messages: list[BaseMessage]) -> None:
    CompactHistoryCases._put_checkpoint(agent_id, messages, version="1")


def test_chat_anchors_past_the_bound_keep_the_referenced_newest_rows(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    """More chat rows than the bound: the oldest prefix plus the referenced
    newest row, in created_at order — never the oldest rows alone."""
    tid = create_agent(db_conn)
    other = create_agent(db_conn)
    ids = [_chat(db_conn, tid, database, event_bus) for _ in range(5)]
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source) "
            "VALUES (%s, '', 'resurrect', 'user') RETURNING id",
            (tid,),
        )
        row = cur.fetchone()
    assert row is not None
    lifecycle_id = row[0]
    other_chat = _chat(db_conn, other, database, event_bus)
    db_conn.commit()

    anchors = list_chat_anchors(
        db_conn, tid, referenced_ids=[ids[4], lifecycle_id, other_chat], limit=2
    )

    assert [anchor.id for anchor in anchors] == [ids[0], ids[1], ids[4]]
    assert all(type(anchor) is ChatAnchor for anchor in anchors)
    assert [anchor.created_at for anchor in anchors] == sorted(a.created_at for a in anchors)
    # No positional prefix: exactly the referenced chat rows.
    assert [a.id for a in list_chat_anchors(db_conn, tid, referenced_ids=[ids[3]], limit=0)] == [
        ids[3]
    ]


def test_all_modern_checkpoint_reads_no_inbound_rows(
    db_conn: psycopg.Connection,
    test_client: TestClient,
    database: Database,
    event_bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tid = create_agent(db_conn)
    inbound_id = _chat(db_conn, tid, database, event_bus)
    db_conn.commit()
    _put(
        tid,
        [
            _modern_inbound(inbound_id, created_at="2026-08-22T19:00:00+00:00"),
            AIMessage(content="ok", additional_kwargs={"ava_created_at": "2026-08-22T19:00:01"}),
        ],
    )

    def no_read(*_args: object, **_kwargs: object) -> list[ChatAnchor]:
        raise AssertionError("an all-modern checkpoint must not read inbound anchors")

    monkeypatch.setattr(gateway_timeline, "list_chat_anchors", no_read)
    resp = test_client.get(f"/api/agents/{tid}/timeline")

    assert resp.status_code == 200
    inbound = [it for it in resp.json()["items"] if it["kind"] == "inbound_chat"]
    assert [it["inbound_id"] for it in inbound] == [inbound_id]


def test_mixed_history_reads_only_the_referenced_anchor(
    db_conn: psycopg.Connection,
    test_client: TestClient,
    database: Database,
    event_bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy sibling after a modern inbound takes that inbound's anchor ts,
    read by id without the agent's older chat rows."""
    tid = create_agent(db_conn)
    _chat(db_conn, tid, database, event_bus)  # older history the render never consumes
    inbound_id = _chat(db_conn, tid, database, event_bus)
    db_conn.commit()
    _put(
        tid,
        [
            _modern_inbound(inbound_id, created_at="2030-01-01T00:00:00+00:00"),
            ToolMessage(
                content="out", tool_call_id="t1", additional_kwargs={"ava_msg_type": "exec_output"}
            ),
        ],
    )
    reads: list[tuple[list[int], int]] = []

    def spy(
        conn: psycopg.Connection, agent_id: int, *, referenced_ids: list[int], limit: int
    ) -> list[ChatAnchor]:
        reads.append((list(referenced_ids), limit))
        return list_chat_anchors(conn, agent_id, referenced_ids=referenced_ids, limit=limit)

    monkeypatch.setattr(gateway_timeline, "list_chat_anchors", spy)
    resp = test_client.get(f"/api/agents/{tid}/timeline")

    assert resp.status_code == 200
    assert reads == [([inbound_id], 0)]
    exec_item = next(it for it in resp.json()["items"] if it["item_id"] == "1.0")
    sibling_ts = datetime.fromisoformat(exec_item["created_at"])
    assert abs((sibling_ts - _created_at(db_conn, inbound_id)).total_seconds()) < 1


def test_legacy_inbounds_align_positionally_through_the_endpoint(
    db_conn: psycopg.Connection, test_client: TestClient, database: Database, event_bus: EventBus
) -> None:
    tid = create_agent(db_conn)
    ids = [_chat(db_conn, tid, database, event_bus) for _ in range(2)]
    db_conn.commit()
    legacy = [
        HumanMessage(
            content=f"legacy {i}",
            additional_kwargs={"ava_msg_type": "inbound", "ava_source": "ui:web"},
        )
        for i in range(2)
    ]
    _put(tid, list(legacy))

    resp = test_client.get(f"/api/agents/{tid}/timeline")

    assert resp.status_code == 200
    inbound = [it for it in resp.json()["items"] if it["kind"] == "inbound_chat"]
    assert [it["inbound_id"] for it in inbound] == ids
    assert [datetime.fromisoformat(it["created_at"]) for it in inbound] == [
        _created_at(db_conn, inbound_id) for inbound_id in ids
    ]
