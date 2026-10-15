from uuid import uuid4

import psycopg

from base.agents.inbox.compact import insert_compact_request_inbound
from base.agents.inbox.insert import insert_inbound_message
from base.agents.inbox.rows import list_pending_inbounds
from base.db import Database
from base.events.live.bus import EventBus
from tests.fixtures.gateway_config import gateway_test_client


def _seed_agent(db_conn: psycopg.Connection) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', 'idling')",
            (new_id,),
        )
    db_conn.commit()
    return new_id


def _set_status(db_conn: psycopg.Connection, inbound_id: int, status: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute("UPDATE inbound_messages SET status = %s WHERE id = %s", (status, inbound_id))
    db_conn.commit()


def test_pending_returns_only_pending_chat_oldest_first(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    tid = _seed_agent(db_conn)
    first = insert_inbound_message(
        db_conn, tid, "first", source="user", bus=event_bus, database=database
    )
    second = insert_inbound_message(
        db_conn, tid, "second", source="agent:3", bus=event_bus, database=database
    )
    claimed = insert_inbound_message(
        db_conn, tid, "being processed", source="user", bus=event_bus, database=database
    )
    _set_status(db_conn, claimed, "claimed")
    done = insert_inbound_message(
        db_conn, tid, "already handled", source="user", bus=event_bus, database=database
    )
    _set_status(db_conn, done, "done")
    insert_compact_request_inbound(db_conn, tid, bus=event_bus, database=database)

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{tid}/pending")

    assert resp.status_code == 200
    items = resp.json()
    assert [it["content"] for it in items] == ["first", "second"]
    assert [it["source"] for it in items] == ["user", "agent:3"]
    assert [it["id"] for it in items] == [first, second]


def test_pending_empty_when_nothing_queued(db_conn: psycopg.Connection) -> None:
    tid = _seed_agent(db_conn)
    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{tid}/pending")
    assert resp.status_code == 200
    assert resp.json() == []


def test_pending_nonexistent_agent_returns_empty(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        resp = client.get("/api/agents/999999/pending")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_pending_inbounds_helper_scopes_by_agent(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    insert_inbound_message(db_conn, a, "for a", source="user", bus=event_bus, database=database)
    insert_inbound_message(db_conn, b, "for b", source="user", bus=event_bus, database=database)
    rows = list_pending_inbounds(db_conn, a)
    assert [r.content for r in rows] == ["for a"]


def _multimodal_payload(urls: list[str]) -> dict[str, object]:
    return {
        "content_blocks": [
            {"type": "text", "text": "look at this"},
            *[{"type": "image_url", "image_url": {"url": url}} for url in urls],
        ]
    }


def test_pending_multimodal_message_carries_image_urls(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    tid = _seed_agent(db_conn)
    url = f"/api/agents/{tid}/uploads/shot.png"
    insert_inbound_message(
        db_conn,
        tid,
        "look at this",
        source="user",
        payload=_multimodal_payload([url]),
        bus=event_bus,
        database=database,
    )
    insert_inbound_message(
        db_conn, tid, "plain text", source="user", bus=event_bus, database=database
    )

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{tid}/pending")

    assert resp.status_code == 200
    items = resp.json()
    assert [it["images"] for it in items] == [[url], None]


def test_pending_keeps_only_renderable_image_refs(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    tid = _seed_agent(db_conn)
    url = f"/api/agents/{tid}/uploads/shot.png"
    payload = _multimodal_payload(
        [
            url,
            "/api/agents/999999/uploads/shot.png",
            f"/api/agents/{tid}/uploads/notes.txt",
            "https://example.com/shot.png",
        ]
    )
    insert_inbound_message(
        db_conn,
        tid,
        "look at this",
        source="user",
        payload=payload,
        bus=event_bus,
        database=database,
    )

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{tid}/pending")

    assert resp.status_code == 200
    assert resp.json()[0]["images"] == [url]


def test_pending_tolerates_malformed_content_blocks(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    tid = _seed_agent(db_conn)
    payload: dict[str, object] = {
        "content_blocks": [
            "not-a-block",
            {"type": "image_url"},
            {"type": "image_url", "image_url": {"url": 7}},
            {"type": "text", "text": "hi"},
        ]
    }
    insert_inbound_message(
        db_conn, tid, "hi", source="user", payload=payload, bus=event_bus, database=database
    )

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{tid}/pending")

    assert resp.status_code == 200
    assert resp.json()[0]["images"] is None


def _active_lease(db_conn: psycopg.Connection, agent_id: int) -> str:
    lease_id = str(uuid4())
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_impersonations"
            "(id, agent_id, source, machine, status, ttl_seconds, expires_at, activated_at) "
            "VALUES (%s, %s, 'external_agent:codex', 'test-machine', 'active', 3600, "
            "clock_timestamp() + interval '1 hour', clock_timestamp())",
            (lease_id, agent_id),
        )
    db_conn.commit()
    return lease_id


def test_active_takeover_hides_transcribed_chat_from_the_strip(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    tid = _seed_agent(db_conn)
    _active_lease(db_conn, tid)
    mid = insert_inbound_message(
        db_conn, tid, "absorbed by the takeover", source="user", bus=event_bus, database=database
    )
    trail = db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_entries "
        "WHERE kind = 'message' AND event_key = 'inbound:' || %s::text",
        (mid,),
    ).fetchone()
    assert trail == (1,)

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{tid}/pending")

    assert resp.status_code == 200
    assert resp.json() == []
    row = db_conn.execute("SELECT status FROM inbound_messages WHERE id = %s", (mid,)).fetchone()
    assert row == ("pending",)


def test_relay_read_hides_while_live_and_reappears_after_release(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    tid = _seed_agent(db_conn)
    mid = insert_inbound_message(
        db_conn, tid, "read before activation", source="user", bus=event_bus, database=database
    )
    lease_id = _active_lease(db_conn, tid)
    db_conn.execute(
        "INSERT INTO agent_impersonation_messages(lease_id, inbound_id) VALUES (%s, %s)",
        (lease_id, mid),
    )
    db_conn.commit()

    with gateway_test_client() as client:
        assert client.get(f"/api/agents/{tid}/pending").json() == []

    db_conn.execute(
        "UPDATE agent_impersonations SET status = 'released', ended_at = clock_timestamp() "
        "WHERE id = %s",
        (lease_id,),
    )
    db_conn.commit()

    with gateway_test_client() as client:
        items = client.get(f"/api/agents/{tid}/pending").json()
    assert [it["id"] for it in items] == [mid]


def test_lapsed_lease_does_not_hide(
    db_conn: psycopg.Connection, database: Database, event_bus: EventBus
) -> None:
    tid = _seed_agent(db_conn)
    mid = insert_inbound_message(
        db_conn, tid, "late again", source="user", bus=event_bus, database=database
    )
    lease_id = _active_lease(db_conn, tid)
    db_conn.execute(
        "INSERT INTO agent_impersonation_messages(lease_id, inbound_id) VALUES (%s, %s)",
        (lease_id, mid),
    )
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at = clock_timestamp() - interval '1 second' "
        "WHERE id = %s",
        (lease_id,),
    )
    db_conn.commit()

    rows = list_pending_inbounds(db_conn, tid)
    assert [r.id for r in rows] == [mid]


def test_acked_message_stays_out_of_the_strip_after_release(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
) -> None:
    tid = _seed_agent(db_conn)
    lease_id = _active_lease(db_conn, tid)
    mid = insert_inbound_message(
        db_conn, tid, "handled", source="user", bus=event_bus, database=database
    )
    db_conn.execute(
        "INSERT INTO agent_impersonation_messages(lease_id, inbound_id) VALUES (%s, %s)",
        (lease_id, mid),
    )
    db_conn.execute("UPDATE inbound_messages SET status = 'done' WHERE id = %s", (mid,))
    db_conn.execute(
        "UPDATE agent_impersonation_messages SET acknowledged_at = clock_timestamp() "
        "WHERE lease_id = %s AND inbound_id = %s",
        (lease_id, mid),
    )
    db_conn.execute(
        "UPDATE agent_impersonations SET status = 'released', ended_at = clock_timestamp() "
        "WHERE id = %s",
        (lease_id,),
    )
    db_conn.commit()

    with gateway_test_client() as client:
        assert client.get(f"/api/agents/{tid}/pending").json() == []
