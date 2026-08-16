"""agent_notices: the unified agent->user queue endpoints (migration 0053).

FastAPI TestClient + real ava_test DB. One table, discriminated by
require_response, carries the whole agent->user queue. Covers:
  - shared.agent_snapshot.select_one exposes the open require_response notices as
    notices_awaiting_response (the "waiting on you" worklist) and counts the open
    FYI notices as unread_notice_count (the badge); FYI content stays off the
    snapshot.
  - GET /api/notices/open — cross-fleet open FYI feed (require_response false),
    priority then newest.
  - GET /api/notices/resolved — cross-fleet resolution history, newest first,
    keyset-paginated, optional require_response filter.
  - POST /api/agents/{id}/notices/{notice_id}/resolve — answer / dismiss / read.
    Marks the notice resolved iff still open; delivers a self-describing
    system-sourced chat inbound: `system:notice-reply` when a reply is supplied,
    `system:notice-dismiss` on a bare dismiss — a notice resolution is a
    notice-system event, not user speech, so no User-role message is consumed.
    409 on not-open or action/kind mismatch; 422 on answer-without-reply.
  - The 'superseded' resolution (migration 0062): when an agent posts a new notice
    via ava.ui.notify(), any previous open notice is auto-resolved as 'superseded'.
"""

from collections.abc import Callable

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.agent_snapshot import select_one


def _seed_agent(db_conn: psycopg.Connection, status: str = "idling") -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', %s)",
            (new_id, status),
        )
    db_conn.commit()
    return new_id


def _insert_notice(
    db_conn: psycopg.Connection,
    agent_id: int,
    title: str,
    *,
    content: str | None = None,
    priority: str = "P2",
    require_response: bool = False,
    blocking: bool = False,
    resolved_at: str | None = None,
    resolution: str | None = None,
    reply: str | None = None,
    task_id: int | None = None,
) -> int:
    """Insert one agent_notices row. `resolved_at`/`resolution` set it resolved;
    the CHECK constraints require a legal (require_response, resolution) pairing,
    so callers pass a coherent combination."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices "
            "(agent_id, local_id, title, content, priority, require_response, blocking, "
            "resolved_at, resolution, reply, task_id) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, %s, %s, %s, %s, %s, %s::timestamptz, %s, %s, %s) RETURNING id",
            (
                agent_id,
                agent_id,
                title,
                content,
                priority,
                require_response,
                blocking,
                resolved_at,
                resolution,
                reply,
                task_id,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        nid = row[0]
    db_conn.commit()
    return nid


def _pending_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, status, content, source FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


# --- snapshot.notices_awaiting_response (require_response worklist) ----------


def test_snapshot_lists_awaiting_oldest_first(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    n1 = _insert_notice(
        db_conn,
        a,
        "deploy to prod?",
        content="A) yes\nB) no",
        priority="P0",
        require_response=True,
        blocking=True,
    )
    n2 = _insert_notice(db_conn, a, "name the branch?", priority="P3", require_response=True)
    # resolved require_response notice is excluded
    _insert_notice(
        db_conn,
        a,
        "already decided",
        require_response=True,
        resolved_at="2026-06-14T01:00:00Z",
        resolution="answered",
        reply="done",
    )
    # an open FYI notice does NOT ride the worklist (it counts as unread instead)
    _insert_notice(db_conn, a, "fyi")

    snap = select_one(db_conn, a)
    assert snap is not None
    assert [n.id for n in snap.notices_awaiting_response] == [n1, n2]
    assert snap.notices_awaiting_response[0].title == "deploy to prod?"
    assert snap.notices_awaiting_response[0].content == "A) yes\nB) no"
    assert snap.notices_awaiting_response[0].priority == "P0"
    assert snap.notices_awaiting_response[0].blocking is True
    assert snap.notices_awaiting_response[1].content is None
    assert snap.notices_awaiting_response[1].priority == "P3"
    assert snap.notices_awaiting_response[1].blocking is False
    # the FYI is the only unread
    assert snap.unread_notice_count == 1


def test_snapshot_awaiting_empty_when_none(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    snap = select_one(db_conn, a)
    assert snap is not None
    assert snap.notices_awaiting_response == []
    assert snap.unread_notice_count == 0


def test_snapshot_scoped_by_agent(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    na = _insert_notice(db_conn, a, "for a", require_response=True)
    _insert_notice(db_conn, b, "for b", require_response=True)
    _insert_notice(db_conn, b, "fyi b")

    snap_a = select_one(db_conn, a)
    assert snap_a is not None
    assert [n.id for n in snap_a.notices_awaiting_response] == [na]
    assert snap_a.unread_notice_count == 0


# --- snapshot.unread_notice_count (open FYI badge) --------------------------


def test_snapshot_counts_unread_fyi(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    _insert_notice(db_conn, a, "milestone 1")
    _insert_notice(db_conn, a, "milestone 2")
    # a read FYI is no longer unread
    _insert_notice(db_conn, a, "old one", resolved_at="2026-06-14T01:00:00Z", resolution="read")
    snap = select_one(db_conn, a)
    assert snap is not None
    assert snap.unread_notice_count == 2


# --- GET /api/notices/open (FYI feed) ---------------------------------------


def test_open_feed_priority_then_newest(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents SET label = 'agent-a' WHERE id = %s", (a,))
    db_conn.commit()
    _insert_notice(db_conn, a, "p2 older", priority="P2")
    p0 = _insert_notice(db_conn, a, "p0", priority="P0")
    _insert_notice(db_conn, a, "p2 newer", priority="P2")

    with TestClient(app) as client:
        resp = client.get("/api/notices/open")
    assert resp.status_code == 200
    data = resp.json()
    # P0 first; within P2, newest before older (LIFO)
    assert [r["title"] for r in data] == ["p0", "p2 newer", "p2 older"]
    assert data[0]["id"] == p0
    assert data[0]["agent_label"] == "agent-a"
    assert data[0]["require_response"] is False
    assert data[0]["resolved_at"] is None


def _seed_task(db_conn: psycopg.Connection, owner: int) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, created_by, owner) "
            "VALUES ('t', 'd', 'user', %s) RETURNING id",
            (owner,),
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    return row[0]


# --- GET /api/notices — the unified inbox feed (Task #1024, Q1=A) ---------


def test_notices_feed_returns_open_awaiting_and_resolved(
    db_conn: psycopg.Connection,
) -> None:
    """One request carries the whole panel: open (FYI) / awaiting
    (require_response) split by kind, plus one keyset page of the resolved
    history and a next_cursor when the page is full."""
    a = _seed_agent(db_conn)
    fyi1 = _insert_notice(db_conn, a, "fyi one", priority="P3")
    fyi2 = _insert_notice(db_conn, a, "fyi two", priority="P0")
    aw1 = _insert_notice(db_conn, a, "decision one", priority="P1", require_response=True)
    aw2 = _insert_notice(db_conn, a, "decision two", priority="P2", require_response=True)
    r1 = _insert_notice(
        db_conn,
        a,
        "done one",
        require_response=True,
        resolved_at="2026-06-14T02:00:00Z",
        resolution="answered",
        reply="ok",
    )
    r2 = _insert_notice(
        db_conn,
        a,
        "done two",
        require_response=True,
        resolved_at="2026-06-14T01:00:00Z",
        resolution="answered",
        reply="ok",
    )
    r3 = _insert_notice(
        db_conn,
        a,
        "done three",
        require_response=True,
        resolved_at="2026-06-14T00:00:00Z",
        resolution="dismissed",
    )

    with TestClient(app) as client:
        feed = client.get("/api/notices").json()

    # open = FYI only, priority then newest
    assert [n["title"] for n in feed["open"]] == ["fyi two", "fyi one"]
    assert [n["id"] for n in feed["open"]] == [fyi2, fyi1]
    # awaiting = require_response only, same sort
    assert [n["title"] for n in feed["awaiting"]] == ["decision one", "decision two"]
    assert [n["id"] for n in feed["awaiting"]] == [aw1, aw2]
    # resolved_page = newest resolution first (default resolved_limit=30
    # returns the whole history here)
    assert [n["id"] for n in feed["resolved_page"]] == [r1, r2, r3]
    # page not full (3 resolved < 30) → end of history
    assert feed["next_cursor"] is None


def test_notices_feed_resolved_page_keyset_cursor(db_conn: psycopg.Connection) -> None:
    """A full resolved page returns a next_cursor; passing it back yields the
    strictly-older page (same keyset semantics as /api/notices/resolved)."""
    a = _seed_agent(db_conn)
    # resolved_limit=2, seed 3 resolved
    for i, ts in enumerate(
        ["2026-06-14T03:00:00Z", "2026-06-14T02:00:00Z", "2026-06-14T01:00:00Z"]
    ):
        _insert_notice(
            db_conn,
            a,
            f"done {i}",
            require_response=True,
            resolved_at=ts,
            resolution="answered",
            reply="ok",
        )

    with TestClient(app) as client:
        page1 = client.get("/api/notices", params={"resolved_limit": 2}).json()
        assert len(page1["resolved_page"]) == 2
        assert page1["next_cursor"] is not None
        # The cursor is page1's LAST row — the next page is strictly older.
        page2 = client.get(
            "/api/notices",
            params={
                "resolved_limit": 2,
                "before_at": page1["next_cursor"]["before_at"],
                "before_id": page1["next_cursor"]["before_id"],
            },
        ).json()
        # The cursor is page1's LAST (oldest) row — the next page is strictly
        # older: the remaining 1 row, and the end of history.
        page2 = client.get(
            "/api/notices",
            params={
                "resolved_limit": 2,
                "before_at": page1["next_cursor"]["before_at"],
                "before_id": page1["next_cursor"]["before_id"],
            },
        ).json()
        assert len(page2["resolved_page"]) == 1
        assert page2["next_cursor"] is None
        assert {n["id"] for n in page1["resolved_page"]} & {
            n["id"] for n in page2["resolved_page"]
        } == set()


def test_notices_feed_422_with_partial_cursor(db_conn: psycopg.Connection) -> None:
    """before_at without before_id (or vice versa) is rejected, same as the
    standalone resolved endpoint."""
    with TestClient(app) as client:
        r = client.get("/api/notices", params={"before_at": "2026-06-14T00:00:00Z"})
        assert r.status_code == 422


def test_notices_feed_excludes_resolved_from_open_lists(
    db_conn: psycopg.Connection,
) -> None:
    """Resolved notices never appear in open or awaiting, whatever their kind."""
    a = _seed_agent(db_conn)
    _insert_notice(db_conn, a, "was fyi", resolved_at="2026-06-14T00:00:00Z", resolution="read")
    _insert_notice(
        db_conn,
        a,
        "was decision",
        require_response=True,
        resolved_at="2026-06-14T00:00:00Z",
        resolution="answered",
        reply="ok",
    )
    with TestClient(app) as client:
        feed = client.get("/api/notices").json()
    assert feed["open"] == []
    assert feed["awaiting"] == []
    assert [n["title"] for n in feed["resolved_page"]] == ["was decision", "was fyi"]


def test_task_id_flows_to_snapshot_and_feed(db_conn: psycopg.Connection) -> None:
    """A notice's task_id rides both the snapshot worklist (require_response) and
    the GET /api/notices/open FYI feed; a notice with no task reads back None."""
    a = _seed_agent(db_conn)
    tid = _seed_task(db_conn, a)
    _insert_notice(db_conn, a, "needs answer", require_response=True, task_id=tid)
    _insert_notice(db_conn, a, "fyi with task", task_id=tid)
    _insert_notice(db_conn, a, "fyi no task")

    snap = select_one(db_conn, a)
    assert snap is not None
    assert [n.task_id for n in snap.notices_awaiting_response] == [tid]

    with TestClient(app) as client:
        feed = client.get("/api/notices/open").json()
    by_title = {r["title"]: r["task_id"] for r in feed}
    assert by_title["fyi with task"] == tid
    assert by_title["fyi no task"] is None


def test_open_feed_excludes_resolved_and_require_response(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    _insert_notice(db_conn, a, "open fyi")
    _insert_notice(db_conn, a, "read fyi", resolved_at="2026-06-14T01:00:00Z", resolution="read")
    # an open require_response notice is NOT in the FYI feed (it rides the snapshot)
    _insert_notice(db_conn, a, "needs answer", require_response=True)
    with TestClient(app) as client:
        data = client.get("/api/notices/open").json()
    assert [r["title"] for r in data] == ["open fyi"]


# --- audit C1: FYI TTL expiry ------------------------------------------------


def _age_notice(db_conn: psycopg.Connection, notice_id: int, days: int) -> None:
    """Backdate a notice's created_at so the TTL rules apply (audit C1)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_notices SET created_at = now() - make_interval(days => %s) WHERE id = %s",
            (days, notice_id),
        )
    db_conn.commit()


def test_open_feed_drops_expired_fyi_and_auto_resolves(
    db_conn: psycopg.Connection,
) -> None:
    """An FYI older than NOTICE_FYI_TTL_DAYS (30) is gone from the open feed and
    is auto-resolved 'read' by the feed query itself — the lazy sweep (audit C1)
    stops the permanent backlog. A recent FYI stays; a require_response notice
    never expires."""
    a = _seed_agent(db_conn)
    _insert_notice(db_conn, a, "fresh fyi")
    stale = _insert_notice(db_conn, a, "stale fyi")
    _age_notice(db_conn, stale, 31)
    req = _insert_notice(db_conn, a, "old but needs answer", require_response=True)
    _age_notice(db_conn, req, 60)

    with TestClient(app) as client:
        feed = client.get("/api/notices/open").json()
    assert [r["title"] for r in feed] == ["fresh fyi"]

    # the sweep auto-resolved the stale FYI as 'read'; the require_response
    # notice is untouched
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT resolution, resolved_at IS NOT NULL FROM agent_notices WHERE id = %s",
            (stale,),
        )
        assert cur.fetchone() == ("read", True)
        cur.execute("SELECT resolved_at FROM agent_notices WHERE id = %s", (req,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] is None


def test_snapshot_unread_count_excludes_expired_fyi(
    db_conn: psycopg.Connection,
) -> None:
    """The unread badge counts only FYIs inside the TTL window (audit C1)."""
    a = _seed_agent(db_conn)
    _insert_notice(db_conn, a, "fresh")
    stale = _insert_notice(db_conn, a, "stale")
    _age_notice(db_conn, stale, 31)
    snap = select_one(db_conn, a)
    assert snap is not None
    assert snap.unread_notice_count == 1


def test_live_excludes_expired_fyi(db_conn: psycopg.Connection) -> None:
    """The IM bridge poll must not see expired FYIs — its cursor stays
    consistent with the feed (audit C1). require_response is exempt."""
    a = _seed_agent(db_conn)
    stale = _insert_notice(db_conn, a, "stale fyi")
    _age_notice(db_conn, stale, 31)
    req = _insert_notice(db_conn, a, "old decision", require_response=True)
    _age_notice(db_conn, req, 60)

    with TestClient(app) as client:
        r = client.get("/api/notices/live", params={"after": 0})
    assert r.status_code == 200
    assert [it["title"] for it in r.json()] == ["old decision"]


def test_open_feed_include_awaiting_excludes_expired_fyi(
    db_conn: psycopg.Connection,
) -> None:
    """The IM bridge's full queue view also drops expired FYIs (audit C1)."""
    a = _seed_agent(db_conn)
    stale = _insert_notice(db_conn, a, "stale fyi")
    _age_notice(db_conn, stale, 31)
    _insert_notice(db_conn, a, "fresh fyi")

    with TestClient(app) as client:
        both = client.get("/api/notices/open", params={"include_awaiting": True}).json()
    assert [it["title"] for it in both] == ["fresh fyi"]


# --- POST .../notices/{id}/resolve : answer ---------------------------------


def test_answer_marks_and_delivers_inbound(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    nid = _insert_notice(
        db_conn, a, "send the records-release email?", require_response=True, blocking=True
    )
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "yes, send it"},
        )
    assert resp.status_code == 201
    assert "status" in resp.json()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT resolved_at, resolution, reply FROM agent_notices WHERE id = %s", (nid,)
        )
        row = cur.fetchone()
    assert row is not None
    resolved_at, resolution, reply = row
    assert resolved_at is not None
    assert resolution == "answered"
    assert reply == "yes, send it"  # cached on the row

    # one self-describing chat inbound, carrying the title and the answer —
    # a notice-system event, not user speech: system-sourced, no User envelope
    rows = _pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:notice-reply"
    assert "send the records-release email?" in inbound_text
    assert "yes, send it" in inbound_text

    # the answered notice drops off the worklist
    snap = select_one(db_conn, a)
    assert snap is not None
    assert snap.notices_awaiting_response == []


def test_answer_without_reply_422(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "q?", require_response=True)
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{a}/notices/{nid}/resolve", json={"action": "answer"})
    assert resp.status_code == 422
    # not resolved, not delivered
    snap = select_one(db_conn, a)
    assert snap is not None
    assert [n.id for n in snap.notices_awaiting_response] == [nid]
    assert _pending_rows(db_conn, a) == []


def test_answer_empty_reply_422(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "q?", require_response=True)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{a}/notices/{nid}/resolve", json={"action": "answer", "reply": "   "}
        )
    assert resp.status_code == 422  # _UserContent strips -> empty -> rejected
    snap = select_one(db_conn, a)
    assert snap is not None
    assert [n.id for n in snap.notices_awaiting_response] == [nid]
    assert _pending_rows(db_conn, a) == []


def test_answer_on_fyi_marks_and_delivers_inbound(db_conn: psycopg.Connection) -> None:
    """Task #1061: an FYI notice is answerable too — the reply marks it
    'answered' and rides `system:notice-reply` to the notice's agent, exactly
    like a require_response answer."""
    a = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "FYI: deploy finished")  # require_response False
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "thanks!"},
        )
    assert resp.status_code == 201
    assert "status" in resp.json()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT resolved_at, resolution, reply FROM agent_notices WHERE id = %s", (nid,)
        )
        row = cur.fetchone()
    assert row is not None
    resolved_at, resolution, reply = row
    assert resolved_at is not None
    assert resolution == "answered"
    assert reply == "thanks!"  # cached on the row

    # one self-describing chat inbound, same shape as the require_response path
    rows = _pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:notice-reply"
    assert "FYI: deploy finished" in inbound_text
    assert "thanks!" in inbound_text


# --- POST .../notices/{id}/resolve : dismiss --------------------------------


def test_dismiss_require_response_delivers_system_note(db_conn: psycopg.Connection) -> None:
    """A dismiss is a system event, not user speech: it wakes the agent (so a
    blocked one stops waiting) but rides `system:notice-dismiss` — no User
    envelope, no user-role message, nothing shaped like a reply request."""
    a = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "name the branch?", require_response=True, blocking=True)
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{a}/notices/{nid}/resolve", json={"action": "dismiss"})
    assert resp.status_code == 201

    with db_conn.cursor() as cur:
        cur.execute("SELECT resolution FROM agent_notices WHERE id = %s", (nid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "dismissed"

    # one self-describing inbound, system-sourced — the claim node envelope-wraps
    # it as "[system] ..." (see shared/envelope.py), never as "User:"
    rows = _pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:notice-dismiss"
    assert "name the branch?" in inbound_text
    assert "dismissed" in inbound_text.lower()


def test_dismiss_on_fyi_is_409(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "fyi")  # require_response False
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{a}/notices/{nid}/resolve", json={"action": "dismiss"})
    assert resp.status_code == 409
    assert _pending_rows(db_conn, a) == []


# --- POST .../notices/{id}/resolve : read -----------------------------------


def test_read_with_reply_marks_and_delivers_inbound(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "migration done", content="14k rows")  # FYI
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{a}/notices/{nid}/resolve",
            json={"action": "read", "reply": "nice, thanks"},
        )
    assert resp.status_code == 201

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT resolved_at, resolution, reply FROM agent_notices WHERE id = %s", (nid,)
        )
        row = cur.fetchone()
    assert row is not None
    resolved_at, resolution, reply = row
    assert resolved_at is not None
    assert resolution == "read"
    assert reply == "nice, thanks"

    # one self-describing chat inbound, carrying the title and the reply —
    # a notice-system event, not user speech: system-sourced, no User envelope
    rows = _pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:notice-reply"
    assert "migration done" in inbound_text
    assert "nice, thanks" in inbound_text

    # the read FYI drops off the unread count
    snap = select_one(db_conn, a)
    assert snap is not None
    assert snap.unread_notice_count == 0


def test_read_without_reply_marks_no_inbound(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "fyi")
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{a}/notices/{nid}/resolve", json={"action": "read"})
    assert resp.status_code == 201

    with db_conn.cursor() as cur:
        cur.execute("SELECT resolved_at, resolution FROM agent_notices WHERE id = %s", (nid,))
        row = cur.fetchone()
    assert row is not None and row[0] is not None and row[1] == "read"
    # read with no reply delivers nothing
    assert _pending_rows(db_conn, a) == []


def test_read_on_require_response_is_409(db_conn: psycopg.Connection) -> None:
    """read applies to an FYI; on a needs-response notice it is a kind mismatch
    -> 409 (use answer/dismiss)."""
    a = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "needs answer", require_response=True)
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{a}/notices/{nid}/resolve", json={"action": "read"})
    assert resp.status_code == 409
    snap = select_one(db_conn, a)
    assert snap is not None
    assert [n.id for n in snap.notices_awaiting_response] == [nid]
    assert _pending_rows(db_conn, a) == []


# --- resolve: not-open / cross-agent / double-resolve -----------------------


def test_resolve_nonexistent_409(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        resp = client.post(f"/api/agents/{a}/notices/999999/resolve", json={"action": "read"})
    assert resp.status_code == 409
    assert _pending_rows(db_conn, a) == []


def test_resolve_twice_second_is_409_and_delivers_once(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "q?", require_response=True)
    with TestClient(app) as client:
        first = client.post(
            f"/api/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "one"},
        )
        second = client.post(
            f"/api/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "two"},
        )
    assert first.status_code == 201
    assert second.status_code == 409
    assert len(_pending_rows(db_conn, a)) == 1  # only the first answer delivered


def test_resolve_cross_agent_path_409(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "for a", require_response=True)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{b}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "x"},
        )
    assert resp.status_code == 409
    # a's notice is untouched
    snap = select_one(db_conn, a)
    assert snap is not None
    assert [n.id for n in snap.notices_awaiting_response] == [nid]


# --- GET /api/notices/resolved (history) ------------------------------------


def test_resolved_lists_fleet_wide_newest_first(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents SET label = 'agent-a' WHERE id = %s", (a,))
        cur.execute("UPDATE agents SET label = 'agent-b' WHERE id = %s", (b,))
    db_conn.commit()
    _insert_notice(
        db_conn,
        a,
        "older",
        require_response=True,
        resolved_at="2026-06-14T01:00:00Z",
        resolution="answered",
        reply="ans-a",
    )
    nnew = _insert_notice(
        db_conn,
        b,
        "newer",
        require_response=True,
        resolved_at="2026-06-14T09:00:00Z",
        resolution="answered",
        reply="ans-b",
    )
    _insert_notice(db_conn, a, "still open", require_response=True)  # excluded

    with TestClient(app) as client:
        resp = client.get("/api/notices/resolved")
    assert resp.status_code == 200
    data = resp.json()
    # newest resolution first; the still-open notice is not present
    assert [r["title"] for r in data] == ["newer", "older"]
    top = data[0]
    assert top["id"] == nnew
    assert top["agent_id"] == b
    assert top["agent_label"] == "agent-b"
    assert top["reply"] == "ans-b"
    assert top["resolution"] == "answered"
    assert top["resolved_at"] is not None


def test_resolved_empty_when_none(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    _insert_notice(db_conn, a, "open only", require_response=True)
    with TestClient(app) as client:
        resp = client.get("/api/notices/resolved")
    assert resp.status_code == 200
    assert resp.json() == []


def test_resolved_require_response_filter(db_conn: psycopg.Connection) -> None:
    """The history filters to one queue's tab: require_response=true is the
    needs-response history, false the FYI history; omit for both."""
    a = _seed_agent(db_conn)
    _insert_notice(
        db_conn,
        a,
        "answered q",
        require_response=True,
        resolved_at="2026-06-14T01:00:00Z",
        resolution="answered",
        reply="ans",
    )
    _insert_notice(
        db_conn,
        a,
        "read fyi",
        resolved_at="2026-06-14T02:00:00Z",
        resolution="read",
    )
    with TestClient(app) as client:
        both = client.get("/api/notices/resolved").json()
        only_q = client.get("/api/notices/resolved?require_response=true").json()
        only_fyi = client.get("/api/notices/resolved?require_response=false").json()
    assert {r["title"] for r in both} == {"answered q", "read fyi"}
    assert [r["title"] for r in only_q] == ["answered q"]
    assert [r["title"] for r in only_fyi] == ["read fyi"]


def test_resolved_respects_limit(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    for i in range(3):
        _insert_notice(
            db_conn,
            a,
            f"n{i}",
            require_response=True,
            resolved_at=f"2026-06-14T0{i + 1}:00:00Z",
            resolution="answered",
            reply="x",
        )
    with TestClient(app) as client:
        data = client.get("/api/notices/resolved?limit=2").json()
    assert len(data) == 2
    # the two most recent (n2 @ 03:00, n1 @ 02:00)
    assert [r["title"] for r in data] == ["n2", "n1"]


def test_resolved_keyset_pages_back(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    for i in range(4):
        _insert_notice(
            db_conn,
            a,
            f"n{i}",
            require_response=True,
            resolved_at=f"2026-06-15T0{i + 1}:00:00Z",
            resolution="answered",
            reply="x",
        )
    with TestClient(app) as client:
        page1 = client.get("/api/notices/resolved?limit=2").json()
        assert [r["title"] for r in page1] == ["n3", "n2"]
        # page two: strictly older than page one's last row
        cursor = page1[-1]
        page2 = client.get(
            "/api/notices/resolved",
            params={
                "limit": 2,
                "before_at": cursor["resolved_at"],
                "before_id": cursor["id"],
            },
        ).json()
    assert [r["title"] for r in page2] == ["n1", "n0"]


def test_resolved_partial_cursor_422(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        only_at = client.get("/api/notices/resolved?before_at=2026-06-15T01:00:00Z")
        only_id = client.get("/api/notices/resolved?before_id=5")
    assert only_at.status_code == 422
    assert only_id.status_code == 422


# --- table CHECK constraints (the load-bearing invariants) -------------------


def test_check_constraints_reject_illegal_states(db_conn: psycopg.Connection) -> None:
    """The four multi-column CHECKs on agent_notices are the invariants the SDK
    and resolve-endpoint guards rest on: they make an illegal queue state
    unrepresentable even if an application-layer guard regressed. Every other test
    here reaches them only through those guards (a 409/422), so this one INSERTs
    each illegal combo straight into the table and asserts the constraint itself
    fires -- if a guard were dropped, this is what still catches the bad write.
    """
    a = _seed_agent(db_conn)
    ts = "2026-06-15T01:00:00Z"

    def _raw_insert(
        *,
        require_response: bool,
        blocking: bool = False,
        resolved_at: str | None = None,
        resolution: str | None = None,
        reply: str | None = None,
    ) -> None:
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_notices "
                "(agent_id, local_id, title, priority, require_response, blocking, "
                "resolved_at, resolution, reply) "
                "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, 'x', 'P2', %s, %s, %s::timestamptz, %s, %s)",
                (a, a, require_response, blocking, resolved_at, resolution, reply),
            )

    def _expect_violation(insert: Callable[[], None]) -> None:
        # `insert` is a zero-arg thunk doing one illegal _raw_insert; assert the
        # CHECK fires, then clear the aborted transaction for the next case.
        with pytest.raises(psycopg.errors.CheckViolation):
            insert()
        db_conn.rollback()

    # Each thunk violates exactly one CHECK (the others kept satisfied) so a
    # failure points at the named constraint, not an accidental second breach.
    # blocking_requires_response: stalled on a reply you never asked for.
    _expect_violation(lambda: _raw_insert(require_response=False, blocking=True))
    # resolution_pair: resolved_at without a resolution, and the reverse.
    _expect_violation(lambda: _raw_insert(require_response=True, resolved_at=ts))
    _expect_violation(lambda: _raw_insert(require_response=True, resolution="answered", reply="x"))
    # resolution_legal: an FYI may be 'answered' (Task #1061 — the user
    # replies to an FYI from Telegram and the text reaches the notice's
    # agent); a needs-response cannot be 'read'.
    _expect_violation(lambda: _raw_insert(require_response=True, resolved_at=ts, resolution="read"))
    # answered_has_reply: an answer must carry text.
    _expect_violation(
        lambda: _raw_insert(require_response=True, resolved_at=ts, resolution="answered")
    )

    # 'superseded' is valid for both kinds (migration 0062).
    _raw_insert(require_response=True, resolved_at=ts, resolution="superseded")
    _raw_insert(require_response=False, resolved_at=ts, resolution="superseded")

    # Positive control: the legal shapes the guards DO produce still insert, so the
    # constraints are proven to reject only the illegal combinations above.
    _raw_insert(require_response=True, resolved_at=ts, resolution="answered", reply="ok")
    _raw_insert(require_response=False, resolved_at=ts, resolution="read")
    _raw_insert(require_response=False, resolved_at=ts, resolution="answered", reply="ok")
    db_conn.commit()


# --- GET /api/notices/live (Task #884: IM bridge poll) -----------------------


def test_live_returns_new_open_notices_oldest_first(
    db_conn: psycopg.Connection,
) -> None:
    """The IM bridge polls with the max id it has seen; /live returns every
    open notice newer than that, both kinds, oldest-first."""
    a1 = _seed_agent(db_conn)
    a2 = _seed_agent(db_conn)
    n1 = _insert_notice(db_conn, a1, "FYI one", require_response=False)
    n2 = _insert_notice(db_conn, a2, "Decision", require_response=True)
    n3 = _insert_notice(db_conn, a1, "FYI two", require_response=False)

    with TestClient(app) as client:
        # full poll from 0
        r = client.get("/api/notices/live", params={"after": 0})
        assert r.status_code == 200
        items = r.json()
        assert [it["id"] for it in items] == [n1, n2, n3]
        assert items[0]["title"] == "FYI one"
        assert items[1]["require_response"] is True
        assert items[1]["agent_id"] == a2

        # incremental poll from n2's id → only n3
        r2 = client.get("/api/notices/live", params={"after": n2})
        assert r2.status_code == 200
        assert [it["id"] for it in r2.json()] == [n3]


def test_live_excludes_resolved(db_conn: psycopg.Connection) -> None:
    """A resolved notice stops appearing in /live — the bridge cursor simply
    never sees it again (idempotent, no tombstone needed)."""
    a1 = _seed_agent(db_conn)
    n1 = _insert_notice(db_conn, a1, "Gone", require_response=False)
    _insert_notice(
        db_conn,
        a1,
        "Stay",
        require_response=False,
        resolved_at="2026-08-06T00:00:00+00:00",
        resolution="read",
    )

    with TestClient(app) as client:
        r = client.get("/api/notices/live", params={"after": 0})
        assert r.status_code == 200
        assert [it["id"] for it in r.json()] == [n1]


def test_open_feed_include_awaiting_returns_both_kinds(
    db_conn: psycopg.Connection,
) -> None:
    """include_awaiting=1 turns /api/notices/open into the full queue view:
    FYI and require_response notices together (Task #941)."""
    a1 = _seed_agent(db_conn)
    _insert_notice(db_conn, a1, "FYI one", require_response=False)
    _insert_notice(db_conn, a1, "Decision", require_response=True)

    with TestClient(app) as client:
        # default: FYI only
        default = client.get("/api/notices/open").json()
        assert [it["title"] for it in default] == ["FYI one"]
        # include_awaiting: both kinds
        both = client.get("/api/notices/open", params={"include_awaiting": True}).json()
        assert {it["title"] for it in both} == {"FYI one", "Decision"}
        # resolved stays excluded
        _insert_notice(
            db_conn,
            a1,
            "Gone",
            require_response=False,
            resolved_at="2026-08-06T00:00:00+00:00",
            resolution="read",
        )
        both2 = client.get("/api/notices/open", params={"include_awaiting": True}).json()
        assert all(it["title"] != "Gone" for it in both2)


def test_live_empty_after_cursor(db_conn: psycopg.Connection) -> None:
    """No newer notices → empty list."""
    a1 = _seed_agent(db_conn)
    n1 = _insert_notice(db_conn, a1, "Only", require_response=False)

    with TestClient(app) as client:
        r = client.get("/api/notices/live", params={"after": n1})
        assert r.status_code == 200
        assert r.json() == []


# ── audit cc-backend-runtime P2: supersede 事件必须用 global id ───────


def test_supersede_publishes_global_notice_id(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a new notice supersedes an open one, the NoticeResolved event must
    carry the GLOBAL notice id (the frontend feed matches on it) while the
    API response's `superseded` list carries LOCAL ids (the SDK's id space).

    Regression: both used the local id, so the frontend could not drop the
    superseded notice from the open feed until the next snapshot refresh.
    """
    from gateway.routers import notices as notices_router

    agent_id = _seed_agent(db_conn)
    # Skew the per-agent LOCAL sequence clear of the GLOBAL one (a resolved
    # notice carrying a high local_id) so the next open notice's LOCAL id
    # differs from its GLOBAL primary key — the test then proves which one each
    # consumer gets.
    #
    # The skew is derived from the current global high-water mark rather than
    # being a fixed 5: the two id spaces are independent, so a constant skew
    # collides whenever this worker's DB happens to hold exactly the number of
    # notices that walks the global sequence onto the same value. The test then
    # failed in its own setup, on nothing but which files xdist's worksteal put
    # on this worker. Deriving it makes the premise hold by construction — the
    # local id lands 100 clear of any global id this test can produce.
    with db_conn.cursor() as cur:
        cur.execute("SELECT coalesce(max(id), 0) FROM agent_notices")
        row = cur.fetchone()
        assert row is not None
        local_skew = int(row[0]) + 100
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, reply, resolved_at, resolution) "
            "VALUES (%s, %s, 'old closed', 'P2', TRUE, 'old reply', now(), 'answered')",
            (agent_id, local_skew),
        )
    db_conn.commit()
    open_id = _insert_notice(db_conn, agent_id, "open notice")
    with db_conn.cursor() as cur:
        cur.execute("SELECT local_id FROM agent_notices WHERE id = %s", (open_id,))
        row = cur.fetchone()
        assert row is not None
        open_local = int(row[0])
    assert open_local != open_id, "test setup: local id must differ from the global id"

    published: list[int] = []

    async def _capture(_agent_id: int, notice_id: int) -> None:
        published.append(notice_id)

    monkeypatch.setattr(notices_router._ops, "publish_notice_resolved", _capture)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{agent_id}/notices",
            json={"title": "new notice", "content": None},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # The SDK-facing list carries LOCAL ids (the SDK's id space)...
    assert body["superseded"] == [open_local]
    # ...while the published event carries the GLOBAL id (the frontend feed
    # matches on it) — the regression that made superseded notices linger in
    # the open feed until the next snapshot refresh.
    assert published == [open_id], f"expected global id {open_id}, got {published}"
