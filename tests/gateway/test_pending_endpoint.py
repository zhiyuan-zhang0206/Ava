"""GET /api/agents/{id}/pending integration tests.

FastAPI TestClient + real ava_test DB. The endpoint returns only the
queued chat inbounds (status='pending', kind='chat'), oldest first —
claimed/done rows and control kinds are excluded, because once claimed a
message shows in the timeline snapshot instead. A multimodal inbound also
carries its image reference urls so the strip can render thumbnails.
"""

from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from gateway.app import app
from shared.db import (
    insert_compact_request_inbound,
    insert_inbound_message,
    list_pending_inbounds,
)


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


def test_pending_returns_only_pending_chat_oldest_first(db_conn: psycopg.Connection) -> None:
    tid = _seed_agent(db_conn)
    first = insert_inbound_message(db_conn, tid, "first", source="user")
    second = insert_inbound_message(db_conn, tid, "second", source="agent:3")
    # claimed + done chat are excluded (they're in the timeline now)
    claimed = insert_inbound_message(db_conn, tid, "being processed", source="user")
    _set_status(db_conn, claimed, "claimed")
    done = insert_inbound_message(db_conn, tid, "already handled", source="user")
    _set_status(db_conn, done, "done")
    # non-chat control kind (compact_request) is excluded even while pending
    insert_compact_request_inbound(db_conn, tid)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{tid}/pending")

    assert resp.status_code == 200
    items = resp.json()
    assert [it["content"] for it in items] == ["first", "second"]
    assert [it["source"] for it in items] == ["user", "agent:3"]
    assert [it["id"] for it in items] == [first, second]


def test_pending_empty_when_nothing_queued(db_conn: psycopg.Connection) -> None:
    tid = _seed_agent(db_conn)
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{tid}/pending")
    assert resp.status_code == 200
    assert resp.json() == []


def test_pending_nonexistent_agent_returns_empty(db_conn: psycopg.Connection) -> None:
    # Lenient read: no 404 precondition (same as the timeline GET).
    with TestClient(app) as client:
        resp = client.get("/api/agents/999999/pending")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_pending_inbounds_helper_scopes_by_agent(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    insert_inbound_message(db_conn, a, "for a", source="user")
    insert_inbound_message(db_conn, b, "for b", source="user")
    rows = list_pending_inbounds(db_conn, a)
    assert [r.content for r in rows] == ["for a"]


def _multimodal_payload(urls: list[str]) -> dict[str, object]:
    """The JSONB shape POST /messages stores for a multimodal message
    (gateway/routers/agents_state.py:_normalize_message_content): the text
    part first, then one image_url block per attached image."""
    return {
        "content_blocks": [
            {"type": "text", "text": "look at this"},
            *[{"type": "image_url", "image_url": {"url": url}} for url in urls],
        ]
    }


def test_pending_multimodal_message_carries_image_urls(db_conn: psycopg.Connection) -> None:
    tid = _seed_agent(db_conn)
    url = f"/api/agents/{tid}/uploads/shot.png"
    insert_inbound_message(
        db_conn, tid, "look at this", source="user", payload=_multimodal_payload([url])
    )
    insert_inbound_message(db_conn, tid, "plain text", source="user")

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{tid}/pending")

    assert resp.status_code == 200
    items = resp.json()
    assert [it["images"] for it in items] == [[url], None]


def test_pending_keeps_only_renderable_image_refs(db_conn: psycopg.Connection) -> None:
    """The same gate POST /messages applies at write time (_validate_image_ref):
    one of this agent's uploads carrying a recognized image suffix. Another
    agent's upload, a non-image suffix, and a non-upload url are all not
    renderable thumbnails."""
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
    insert_inbound_message(db_conn, tid, "look at this", source="user", payload=payload)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{tid}/pending")

    assert resp.status_code == 200
    assert resp.json()[0]["images"] == [url]


def test_pending_tolerates_malformed_content_blocks(db_conn: psycopg.Connection) -> None:
    """A payload shaped differently than today's writer degrades to "no
    images" instead of failing the whole queue read."""
    tid = _seed_agent(db_conn)
    payload: dict[str, object] = {
        "content_blocks": [
            "not-a-block",
            {"type": "image_url"},
            {"type": "image_url", "image_url": {"url": 7}},
            {"type": "text", "text": "hi"},
        ]
    }
    insert_inbound_message(db_conn, tid, "hi", source="user", payload=payload)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{tid}/pending")

    assert resp.status_code == 200
    assert resp.json()[0]["images"] is None


def _active_lease(db_conn: psycopg.Connection, agent_id: int) -> str:
    """A minimal unexpired active lease row (the takeover's delivery authority)."""
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
) -> None:
    """#3683: a chat the takeover trail has transcribed must not show in both
    the timeline and the strip. The inbound trigger stamps the session trail
    (what the timeline renders); the strip drops the row; the row itself
    stays pending for the ACK/release machinery."""
    tid = _seed_agent(db_conn)
    _active_lease(db_conn, tid)
    mid = insert_inbound_message(db_conn, tid, "absorbed by the takeover", source="user")
    trail = db_conn.execute(
        "SELECT count(*) FROM agent_impersonation_entries "
        "WHERE kind = 'message' AND event_key = 'inbound:' || %s::text",
        (mid,),
    ).fetchone()
    assert trail == (1,)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{tid}/pending")

    assert resp.status_code == 200
    assert resp.json() == []
    row = db_conn.execute("SELECT status FROM inbound_messages WHERE id = %s", (mid,)).fetchone()
    assert row == ("pending",)


def test_relay_read_hides_while_live_and_reappears_after_release(
    db_conn: psycopg.Connection,
) -> None:
    """J1 evidence alone (read by the relay, no trail) hides the row while
    the lease is live; a release that never ACKed must surface it again for
    the native agent — delivery evidence counts only while the lease is
    alive."""
    tid = _seed_agent(db_conn)
    mid = insert_inbound_message(db_conn, tid, "read before activation", source="user")
    lease_id = _active_lease(db_conn, tid)
    db_conn.execute(
        "INSERT INTO agent_impersonation_messages(lease_id, inbound_id) VALUES (%s, %s)",
        (lease_id, mid),
    )
    db_conn.commit()

    with TestClient(app) as client:
        assert client.get(f"/api/agents/{tid}/pending").json() == []

    db_conn.execute(
        "UPDATE agent_impersonations SET status = 'released', ended_at = clock_timestamp() "
        "WHERE id = %s",
        (lease_id,),
    )
    db_conn.commit()

    with TestClient(app) as client:
        items = client.get(f"/api/agents/{tid}/pending").json()
    assert [it["id"] for it in items] == [mid]


def test_lapsed_lease_does_not_hide(db_conn: psycopg.Connection) -> None:
    """Delivery authority dies with the lease deadline: an active-status row
    past its expiry must not hide anything (the predicate requires
    expires_at > now()). Read through the helper — the app-level reaper
    reaps a lapsed lease and posts its terminal note, which would race this
    assertion."""
    tid = _seed_agent(db_conn)
    mid = insert_inbound_message(db_conn, tid, "late again", source="user")
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
) -> None:
    """ACK is `done`: excluded as an ordinary settled row, before and after
    the session ends."""
    tid = _seed_agent(db_conn)
    lease_id = _active_lease(db_conn, tid)
    mid = insert_inbound_message(db_conn, tid, "handled", source="user")
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

    with TestClient(app) as client:
        assert client.get(f"/api/agents/{tid}/pending").json() == []
