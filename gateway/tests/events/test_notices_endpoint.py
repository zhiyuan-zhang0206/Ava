import json
from uuid import uuid4

import psycopg

from base.agents.observation.snapshot import select_one
from base.api_contracts.idempotency import keyed_headers
from base.config import settings
from base.events.live.bus import EventBus, event_bus_config_from_settings
from base.lm.catalog import ModelCatalog
from tests.fixtures.gateway_config import gateway_test_client


def seed_agent(db_conn: psycopg.Connection, status: str = "idling") -> int:
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


def insert_notice(
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
    expire_at: str | None = None,
) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices "
            "(agent_id, local_id, title, content, priority, require_response, blocking, "
            "resolved_at, resolution, reply, task_id, expire_at) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, %s, %s, %s, %s, %s, %s::timestamptz, %s, %s, %s, COALESCE(%s::timestamptz, now() + interval '1 day')) RETURNING id",
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
                expire_at,
            ),
        )
        row = cur.fetchone()
        assert row is not None
        nid = row[0]
    db_conn.commit()
    return nid


def pending_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT kind, status, content, source FROM inbound_messages "
            "WHERE agent_id = %s ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


def test_snapshot_lists_awaiting_oldest_first(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    n1 = insert_notice(
        db_conn,
        a,
        "deploy to prod?",
        content="A) yes\nB) no",
        priority="P0",
        require_response=True,
        blocking=True,
    )
    n2 = insert_notice(db_conn, a, "name the branch?", priority="P3", require_response=True)
    insert_notice(
        db_conn,
        a,
        "already decided",
        require_response=True,
        resolved_at="2026-06-14T01:00:00Z",
        resolution="answered",
        reply="done",
    )
    insert_notice(db_conn, a, "fyi")

    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert [n.id for n in snap.notices_awaiting_response] == [n1, n2]
    assert snap.notices_awaiting_response[0].title == "deploy to prod?"
    assert snap.notices_awaiting_response[0].content == "A) yes\nB) no"
    assert snap.notices_awaiting_response[0].priority == "P0"
    assert snap.notices_awaiting_response[0].blocking is True
    assert snap.notices_awaiting_response[1].content is None
    assert snap.notices_awaiting_response[1].priority == "P3"
    assert snap.notices_awaiting_response[1].blocking is False
    assert snap.unread_notice_count == 1


def test_snapshot_awaiting_empty_when_none(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert snap.notices_awaiting_response == []
    assert snap.unread_notice_count == 0


def test_snapshot_scoped_by_agent(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    b = seed_agent(db_conn)
    na = insert_notice(db_conn, a, "for a", require_response=True)
    insert_notice(db_conn, b, "for b", require_response=True)
    insert_notice(db_conn, b, "fyi b")

    snap_a = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap_a is not None
    assert [n.id for n in snap_a.notices_awaiting_response] == [na]
    assert snap_a.unread_notice_count == 0


def test_snapshot_counts_unread_fyi(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    insert_notice(db_conn, a, "milestone 1")
    insert_notice(db_conn, a, "milestone 2")
    insert_notice(db_conn, a, "old one", resolved_at="2026-06-14T01:00:00Z", resolution="read")
    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert snap.unread_notice_count == 2


def test_open_feed_priority_then_newest(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents SET label = 'agent-a' WHERE id = %s", (a,))
    db_conn.commit()
    insert_notice(db_conn, a, "p2 older", priority="P2")
    p0 = insert_notice(db_conn, a, "p0", priority="P0")
    insert_notice(db_conn, a, "p2 newer", priority="P2")

    with gateway_test_client() as client:
        resp = client.get("/api/notices/open")
    assert resp.status_code == 200
    data = resp.json()
    assert [r["title"] for r in data] == ["p0", "p2 newer", "p2 older"]
    assert data[0]["id"] == p0
    assert data[0]["agent_label"] == "agent-a"
    assert data[0]["require_response"] is False
    assert data[0]["resolved_at"] is None


def _seed_task(
    db_conn: psycopg.Connection,
    owner: int,
    *,
    title: str = "t",
    status: str = "in_progress",
    reminder_count: int = 0,
) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, created_by, owner, status, reminder_count) "
            "VALUES (%s, 'd', 'user', %s, %s, %s) RETURNING id",
            (title, owner, status, reminder_count),
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    return row[0]


def test_notices_feed_returns_open_awaiting_and_resolved(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    fyi1 = insert_notice(db_conn, a, "fyi one", priority="P3")
    fyi2 = insert_notice(db_conn, a, "fyi two", priority="P0")
    aw1 = insert_notice(db_conn, a, "decision one", priority="P1", require_response=True)
    aw2 = insert_notice(db_conn, a, "decision two", priority="P2", require_response=True)
    r1 = insert_notice(
        db_conn,
        a,
        "done one",
        require_response=True,
        resolved_at="2026-06-14T02:00:00Z",
        resolution="answered",
        reply="ok",
    )
    r2 = insert_notice(
        db_conn,
        a,
        "done two",
        require_response=True,
        resolved_at="2026-06-14T01:00:00Z",
        resolution="answered",
        reply="ok",
    )
    r3 = insert_notice(
        db_conn,
        a,
        "done three",
        require_response=True,
        resolved_at="2026-06-14T00:00:00Z",
        resolution="dismissed",
    )

    with gateway_test_client() as client:
        feed = client.get("/api/notices").json()

    assert [n["title"] for n in feed["open"]] == ["fyi two", "fyi one"]
    assert [n["id"] for n in feed["open"]] == [fyi2, fyi1]
    assert [n["title"] for n in feed["awaiting"]] == ["decision one", "decision two"]
    assert [n["id"] for n in feed["awaiting"]] == [aw1, aw2]
    assert [n["id"] for n in feed["resolved_page"]] == [r1, r2, r3]
    assert feed["next_cursor"] is None


def test_notices_feed_resolved_page_keyset_cursor(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    for i, ts in enumerate(
        ["2026-06-14T03:00:00Z", "2026-06-14T02:00:00Z", "2026-06-14T01:00:00Z"]
    ):
        insert_notice(
            db_conn,
            a,
            f"done {i}",
            require_response=True,
            resolved_at=ts,
            resolution="answered",
            reply="ok",
        )

    with gateway_test_client() as client:
        page1 = client.get("/api/notices", params={"resolved_limit": 2}).json()
        assert len(page1["resolved_page"]) == 2
        assert page1["next_cursor"] is not None
        page2 = client.get(
            "/api/notices",
            params={
                "resolved_limit": 2,
                "before_at": page1["next_cursor"]["before_at"],
                "before_id": page1["next_cursor"]["before_id"],
            },
        ).json()
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
    with gateway_test_client() as client:
        r = client.get("/api/notices", params={"before_at": "2026-06-14T00:00:00Z"})
        assert r.status_code == 422


def test_notices_feed_excludes_resolved_from_open_lists(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    insert_notice(db_conn, a, "was fyi", resolved_at="2026-06-14T00:00:00Z", resolution="read")
    insert_notice(
        db_conn,
        a,
        "was decision",
        require_response=True,
        resolved_at="2026-06-14T00:00:00Z",
        resolution="answered",
        reply="ok",
    )
    with gateway_test_client() as client:
        feed = client.get("/api/notices").json()
    assert feed["open"] == []
    assert feed["awaiting"] == []
    assert [n["title"] for n in feed["resolved_page"]] == ["was decision", "was fyi"]


def test_task_id_flows_to_snapshot_and_feed(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    tid = _seed_task(db_conn, a)
    insert_notice(db_conn, a, "needs answer", require_response=True, task_id=tid)
    insert_notice(db_conn, a, "fyi with task", task_id=tid)
    insert_notice(db_conn, a, "fyi no task")

    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert [n.task_id for n in snap.notices_awaiting_response] == [tid]

    with gateway_test_client() as client:
        feed = client.get("/api/notices/open").json()
    by_title = {r["title"]: r["task_id"] for r in feed}
    assert by_title["fyi with task"] == tid
    assert by_title["fyi no task"] is None


def test_open_feed_excludes_resolved_and_require_response(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    insert_notice(db_conn, a, "open fyi")
    insert_notice(db_conn, a, "read fyi", resolved_at="2026-06-14T01:00:00Z", resolution="read")
    insert_notice(db_conn, a, "needs answer", require_response=True)
    with gateway_test_client() as client:
        data = client.get("/api/notices/open").json()
    assert [r["title"] for r in data] == ["open fyi"]


def _age_notice(db_conn: psycopg.Connection, notice_id: int, days: int) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_notices SET created_at = now() - make_interval(days => %s) WHERE id = %s",
            (days, notice_id),
        )
    db_conn.commit()


def test_open_feed_drops_expired_fyi_and_auto_resolves(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    insert_notice(db_conn, a, "fresh fyi")
    stale = insert_notice(db_conn, a, "stale fyi")
    _age_notice(db_conn, stale, 31)
    req = insert_notice(db_conn, a, "old but needs answer", require_response=True)
    _age_notice(db_conn, req, 60)

    with gateway_test_client() as client:
        feed = client.get("/api/notices/open").json()
    assert [r["title"] for r in feed] == ["fresh fyi"]

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
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    insert_notice(db_conn, a, "fresh")
    stale = insert_notice(db_conn, a, "stale")
    _age_notice(db_conn, stale, 31)
    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert snap.unread_notice_count == 1


def test_live_excludes_expired_fyi(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    stale = insert_notice(db_conn, a, "stale fyi")
    _age_notice(db_conn, stale, 31)
    req = insert_notice(db_conn, a, "old decision", require_response=True)
    _age_notice(db_conn, req, 60)

    with gateway_test_client() as client:
        r = client.get("/api/notices/live", params={"after": 0})
    assert r.status_code == 200
    assert [it["title"] for it in r.json()] == ["old decision"]


def test_open_feed_include_awaiting_excludes_expired_fyi(
    db_conn: psycopg.Connection,
) -> None:
    a = seed_agent(db_conn)
    stale = insert_notice(db_conn, a, "stale fyi")
    _age_notice(db_conn, stale, 31)
    insert_notice(db_conn, a, "fresh fyi")

    with gateway_test_client() as client:
        both = client.get("/api/notices/open", params={"include_awaiting": True}).json()
    assert [it["title"] for it in both] == ["fresh fyi"]


def test_open_feed_agent_filter_returns_only_that_agents_notices(
    db_conn: psycopg.Connection,
) -> None:
    mine = seed_agent(db_conn)
    other = seed_agent(db_conn)
    insert_notice(db_conn, mine, "my question", require_response=True)
    insert_notice(db_conn, other, "their fyi")

    with gateway_test_client() as client:
        own = client.get(
            "/api/notices/open", params={"agent_id": mine, "include_awaiting": True}
        ).json()
        nothing = client.get("/api/notices/open", params={"agent_id": 999999}).json()
        invalid = client.get("/api/notices/open", params={"agent_id": 0})
    assert [it["title"] for it in own] == ["my question"]
    assert nothing == []
    assert invalid.status_code == 422


def test_answer_marks_and_delivers_inbound(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(
        db_conn, a, "send the records-release email?", require_response=True, blocking=True
    )
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "yes, send it"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
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
    assert reply == "yes, send it"

    rows = pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:notice-reply"
    assert "send the records-release email?" in inbound_text
    assert "yes, send it" in inbound_text

    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert snap.notices_awaiting_response == []


def test_answer_without_reply_422(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "q?", require_response=True)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 422
    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert [n.id for n in snap.notices_awaiting_response] == [nid]
    assert pending_rows(db_conn, a) == []


def test_answer_empty_reply_422(
    db_conn: psycopg.Connection, *, model_catalog: ModelCatalog
) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "q?", require_response=True)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "   "},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 422
    snap = select_one(
        db_conn,
        a,
        catalog=model_catalog,
        default_model_reader=lambda: settings.lm.llm_model,
    )
    assert snap is not None
    assert [n.id for n in snap.notices_awaiting_response] == [nid]
    assert pending_rows(db_conn, a) == []


def test_answer_on_fyi_marks_and_delivers_inbound(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "FYI: deploy finished")
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "thanks!"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
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
    assert reply == "thanks!"

    rows = pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:notice-reply"
    assert "FYI: deploy finished" in inbound_text
    assert "thanks!" in inbound_text


def test_dismiss_require_response_delivers_system_note(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "name the branch?", require_response=True, blocking=True)
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "dismiss"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 201

    with db_conn.cursor() as cur:
        cur.execute("SELECT resolution FROM agent_notices WHERE id = %s", (nid,))
        row = cur.fetchone()
    assert row is not None and row[0] == "dismissed"

    rows = pending_rows(db_conn, a)
    assert len(rows) == 1
    kind, _status, inbound_text, source = rows[0]
    assert kind == "chat"
    assert source == "system:notice-dismiss"
    assert "name the branch?" in inbound_text
    assert "dismissed" in inbound_text.lower()


def test_dismiss_on_fyi_is_409(db_conn: psycopg.Connection) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "fyi")
    with gateway_test_client() as client:
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "dismiss"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 409
    assert pending_rows(db_conn, a) == []


class _RecordingBus(EventBus):
    def __init__(self) -> None:
        super().__init__(event_bus_config_from_settings())
        self.payloads: list[dict[str, object]] = []

    async def publish_best_effort(
        self, payload: str, *, channel: str | None = None, context: str = ""
    ) -> int | None:
        self.payloads.append(json.loads(payload))
        return 1


def test_answer_event_carries_resolution_fields(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    a = seed_agent(db_conn)
    nid = insert_notice(db_conn, a, "ship it?", require_response=True)
    bus = _RecordingBus()
    with gateway_test_client() as client:
        client.app.state.bus = bus
        resp = client.post(
            f"/api/keyed/v1/agents/{a}/notices/{nid}/resolve",
            json={"action": "answer", "reply": "yes"},
            headers={"Idempotency-Key": str(uuid4()), "Idempotency-Scope": "principal-v1"},
        )
    assert resp.status_code == 201
    resolved = [e for e in bus.payloads if e.get("role") == "notice_resolved"]
    assert len(resolved) == 1
    event = resolved[0]
    assert event["notice_id"] == nid
    assert event["resolution"] == "answered"
    assert event["require_response"] is True
    assert event["had_reply"] is True
    assert isinstance(event["age_seconds"], int) and event["age_seconds"] >= 0
    assert event["superseded_by"] is None


def test_supersede_event_carries_superseded_by(
    db_conn: psycopg.Connection, event_bus: EventBus
) -> None:
    a = seed_agent(db_conn)
    bus = _RecordingBus()
    with gateway_test_client() as client:
        client.app.state.bus = bus
        first = client.post(
            f"/api/agents/{a}/notices",
            json={"title": "first", "require_response": True},
            headers=keyed_headers("supersede-first"),
        )
        second = client.post(
            f"/api/agents/{a}/notices",
            json={"title": "second", "require_response": True},
            headers=keyed_headers("supersede-second"),
        )
    assert first.status_code == second.status_code == 201
    with db_conn.cursor() as cur:
        cur.execute("SELECT id, title FROM agent_notices WHERE agent_id = %s ORDER BY id", (a,))
        ids = {title: notice_id for notice_id, title in cur.fetchall()}
    event = next(
        e
        for e in bus.payloads
        if e.get("role") == "notice_resolved" and e.get("notice_id") == ids["first"]
    )
    assert event["resolution"] == "superseded"
    assert event["require_response"] is True
    assert event["had_reply"] is False
    assert event["superseded_by"] == ids["second"]
