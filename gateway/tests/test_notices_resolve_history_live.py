"""The resolve/read action endpoints and their surfaces: read/resolve/answer routing, live polling, supersede and expire_at TTL defaults; split from gateway/tests/test_notices_endpoint.py (task #4922)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.agents.observation.snapshot import select_one
from base.config import settings
from gateway.app import app
from gateway.tests.test_notices_endpoint import _insert_notice, _pending_rows, _seed_agent

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


def test_read_twice_second_is_silent_201(db_conn: psycopg.Connection) -> None:
    """Mark read on an already-read FYI notice silently succeeds (user ruling
    2026-08-28): the second read is a 201 no-op — no error, no new inbound,
    the row keeps its original resolution."""
    a = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "fyi")
    with TestClient(app) as client:
        first = client.post(f"/api/agents/{a}/notices/{nid}/resolve", json={"action": "read"})
        second = client.post(f"/api/agents/{a}/notices/{nid}/resolve", json={"action": "read"})
    assert first.status_code == 201
    assert second.status_code == 201
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT resolved_at, resolution, reply FROM agent_notices WHERE id = %s", (nid,)
        )
        row = cur.fetchone()
    assert row is not None and row[0] is not None and row[1] == "read" and row[2] is None
    # bare reads deliver nothing, the second one included
    assert _pending_rows(db_conn, a) == []


def test_read_with_reply_on_already_resolved_delivers_note(
    db_conn: psycopg.Connection,
) -> None:
    """A note attached to a read on an already-resolved notice still reaches
    the agent (the close itself is a no-op) — user input is never silently
    dropped, matching the normal read-with-note path."""
    a = _seed_agent(db_conn)
    nid = _insert_notice(db_conn, a, "migration done")
    with TestClient(app) as client:
        first = client.post(f"/api/agents/{a}/notices/{nid}/resolve", json={"action": "read"})
        second = client.post(
            f"/api/agents/{a}/notices/{nid}/resolve",
            json={"action": "read", "reply": "noted"},
        )
    assert first.status_code == 201
    assert second.status_code == 201
    # exactly one inbound — from the note, system:notice-reply, carrying title + note
    rows = _pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:notice-reply"
    assert "migration done" in inbound_text
    assert "noted" in inbound_text
    # the row's original resolution is untouched (no clobbered reply cache)
    with db_conn.cursor() as cur:
        cur.execute("SELECT resolution, reply FROM agent_notices WHERE id = %s", (nid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "read" and row[1] is None


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


def test_open_default_limit_comes_from_display_config(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    """The implicit open-feed cap is ``settings.display.notices_open_default_limit``
    (``AVA_NOTICES_OPEN_DEFAULT_LIMIT``); the literal 200 is only that field's
    default, not a hard-coded page size."""
    from base.config import settings

    monkeypatch.setattr(settings.display, "notices_open_default_limit", 2)
    a = _seed_agent(db_conn)
    _insert_notice(db_conn, a, "older")
    _insert_notice(db_conn, a, "newer")
    _insert_notice(db_conn, a, "newest")

    with TestClient(app) as client:
        data = client.get("/api/notices/open").json()
    assert [r["title"] for r in data] == ["newest", "newer"]


def test_feed_defaults_come_from_display_config(
    monkeypatch: pytest.MonkeyPatch, db_conn: psycopg.Connection
) -> None:
    """The unified feed's implicit open cap and resolved page follow the
    display-config fields."""
    from base.config import settings

    monkeypatch.setattr(settings.display, "notices_open_default_limit", 1)
    monkeypatch.setattr(settings.display, "notices_resolved_default_page", 1)
    a = _seed_agent(db_conn)
    _insert_notice(db_conn, a, "open older")
    _insert_notice(db_conn, a, "open newer")
    _insert_notice(
        db_conn,
        a,
        "resolved r0",
        require_response=True,
        resolved_at="2026-06-14T01:00:00Z",
        resolution="answered",
        reply="x",
    )
    _insert_notice(
        db_conn,
        a,
        "resolved r1",
        require_response=True,
        resolved_at="2026-06-14T02:00:00Z",
        resolution="answered",
        reply="x",
    )

    with TestClient(app) as client:
        feed = client.get("/api/notices").json()
    assert [r["title"] for r in feed["open"]] == ["open newer"]
    assert [r["title"] for r in feed["resolved_page"]] == ["resolved r1"]


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
                "resolved_at, resolution, reply, expire_at) "
                "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, 'x', 'P2', %s, %s, %s::timestamptz, %s, %s, now() + interval '1 day')",
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
    # 'expired' is valid for both kinds.
    _raw_insert(require_response=True, resolved_at=ts, resolution="expired")
    _raw_insert(require_response=False, resolved_at=ts, resolution="expired")

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


# -- audit cc-backend-runtime P2: supersede events must use the global id ---


def test_supersede_publishes_global_notice_id(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a new notice supersedes an open one, the NoticeResolved event must
    carry the GLOBAL notice id (the frontend feed matches on it) while the
    API response's `superseded` list carries LOCAL ids (the SDK's id space).

    Regression: both used the local id, so the frontend could not drop the
    superseded notice from the open feed until the next snapshot refresh.
    """
    from ops import lifecycle as ops_lifecycle

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
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, reply, resolved_at, resolution, expire_at) "
            "VALUES (%s, %s, 'old closed', 'P2', TRUE, 'old reply', now(), 'answered', now() + interval '1 day')",
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

    async def _capture(_bus: object, _agent_id: int, notice_id: int) -> None:
        published.append(notice_id)

    monkeypatch.setattr(ops_lifecycle, "publish_notice_resolved", _capture)
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


# --- expire_at TTL tests ----------------------------------------------------


def test_post_notice_sets_default_expire_at(db_conn: psycopg.Connection) -> None:
    """POST without expire_at sets default expire_at based on daemon setting."""
    aid = _seed_agent(db_conn)
    before = datetime.now(UTC)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/notices",
            json={"title": "default expiry notice", "content": "detail"},
        )
    assert resp.status_code == 201
    after = datetime.now(UTC)

    with db_conn.cursor() as cur:
        cur.execute("SELECT expire_at FROM agent_notices WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
    assert row is not None
    expire_at = row[0]
    expected_sec = settings.daemon.notice_ttl_limit_seconds
    assert (
        before + timedelta(seconds=expected_sec - 5)
        <= expire_at
        <= after + timedelta(seconds=expected_sec + 5)
    )


def test_post_notice_with_explicit_expire_at(db_conn: psycopg.Connection) -> None:
    """POST with explicit expire_at stores the requested timestamp."""
    aid = _seed_agent(db_conn)
    target = datetime.now(UTC) + timedelta(hours=2)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/notices",
            json={
                "title": "explicit expiry notice",
                "expire_at": target.isoformat(),
            },
        )
    assert resp.status_code == 201

    with db_conn.cursor() as cur:
        cur.execute("SELECT expire_at FROM agent_notices WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
    assert row is not None
    expire_at = row[0]
    assert abs((expire_at - target).total_seconds()) < 2.0


def test_post_notice_clamps_expire_at_when_exceeding_limit(db_conn: psycopg.Connection) -> None:
    """POST with explicit expire_at exceeding the configured limit is clamped."""
    aid = _seed_agent(db_conn)
    now = datetime.now(UTC)
    target = now + timedelta(days=10)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/notices",
            json={
                "title": "exceeding expiry notice",
                "expire_at": target.isoformat(),
            },
        )
    assert resp.status_code == 201

    with db_conn.cursor() as cur:
        cur.execute("SELECT expire_at FROM agent_notices WHERE agent_id = %s", (aid,))
        row = cur.fetchone()
    assert row is not None
    expire_at = row[0]
    expected_max = now + timedelta(seconds=settings.daemon.notice_ttl_limit_seconds)
    assert abs((expire_at - expected_max).total_seconds()) < 5.0


def test_post_notice_past_expire_at_is_422(db_conn: psycopg.Connection) -> None:
    """POST with expire_at in the past returns 422."""
    aid = _seed_agent(db_conn)
    past = datetime.now(UTC) - timedelta(minutes=10)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/agents/{aid}/notices",
            json={
                "title": "past expiry notice",
                "expire_at": past.isoformat(),
            },
        )
    assert resp.status_code == 422
    assert "expire_at is in the past" in resp.json()["detail"]
