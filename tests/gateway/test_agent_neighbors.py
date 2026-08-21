"""GET /api/agents/{id}/neighbors integration tests.

FastAPI TestClient + real ava_test DB + FakeLoki. Task #180 (LGTM cutover):
the retired `agent_neighbors` SQL function read the frozen `events` table
and silently returned no peers; the walk now runs in Python over the event
stream (gateway/neighbors.py) — audit edge rows stitch the frozen PG archive
(ts below the freeze boundary) with the Loki live tail. Covered here end to
end: undirected ties, permanent lineage weights (spawn/fork/resurrect, no
time decay) vs decaying message weights (send_message, EXP(-k*days)),
per-hop gamma decay, terminated inclusion, limit, self/root exclusions, and
the archive+Loki merge on the same pair.
"""

import math

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway import loki_events
from gateway.app import app
from tests.gateway.loki_fake import FakeLoki


@pytest.fixture(autouse=True)
def fake_loki(monkeypatch: pytest.MonkeyPatch) -> FakeLoki:
    """Route all loki_events calls through an in-memory fake; each test gets
    an empty store and adds its own rows."""
    fake = FakeLoki()
    monkeypatch.setattr(loki_events, "query_events", fake.query_events)
    monkeypatch.setattr(loki_events, "count_events", fake.count_events)
    return fake


def _seed_agent(db_conn: psycopg.Connection, *, status: str = "running") -> int:
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


def _event(
    fake: FakeLoki,
    *,
    event_type: str,
    agent_id: int,
    target: int | None,
    days_ago: float = 0.0,
    count: int = 1,
) -> None:
    """Add `count` live-tail audit events for an (agent_id, target) pair at
    a fixed age. agent_id and target are the two endpoints of the
    inter-agent tie; the walk keys purely on them, so the source string is
    irrelevant to the graph."""
    for _ in range(count):
        fake.add(
            event=event_type,
            agent_id=agent_id,
            target_agent_id=target,
            category="audit",
            ts_offset_hours=days_ago * 24.0,
        )


def _archive_event(
    db_conn: psycopg.Connection,
    *,
    event_type: str,
    agent_id: int,
    target: int | None,
    age_hours: float = 1.0,
) -> None:
    """Insert one ARCHIVE-era audit event into the frozen PG `events`
    archive (aged into the past)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events "
            "(ts, agent_id, event_name, source, target_agent_id, machine, process, category, level) "
            "VALUES (now() - %s * interval '1 hour', %s, %s, 'test', %s, "
            "'test', 'test', 'audit', 'info')",
            (age_hours, agent_id, event_type, target),
        )
    db_conn.commit()


def _archive_boundary_anchor(db_conn: psycopg.Connection) -> None:
    """Pin the archive's freeze point to ~now: the read partitions the
    timeline at max(events.ts), so a test mixing archive-era rows (old ts)
    with Loki rows (ts≈now) must insert a boundary anchor — otherwise the
    oldest archive row would BE the boundary and sit outside its own window
    (`ts < boundary`). A telemetry row serves as the freeze marker."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events "
            "(ts, agent_id, event_name, source, machine, process, category, level) "
            "VALUES (now(), NULL, 'turn_end', 'test', 'test', 'test', 'telemetry', 'info')"
        )
    db_conn.commit()


def _neighbors(client: TestClient, agent_id: int, **params: int) -> list[dict]:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    resp = client.get(f"/api/agents/{agent_id}/neighbors", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["neighbors"]


def test_direct_ties_both_directions_self_and_root_excluded(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # b messaged a (tie a-b); a spawned c (tie a-c). Direction does not matter.
    _event(fake_loki, event_type="send_message", agent_id=b, target=a)
    _event(fake_loki, event_type="spawn", agent_id=c, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    ids = {r["agent_id"] for r in rows}  # pyright: ignore[reportUnknownVariableType]
    assert ids == {b, c}  # root a excluded; both neighbors found regardless of direction
    assert all(r["depth"] == 1 for r in rows)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


def test_lineage_and_message_equal_at_zero_age(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # At age 0 the message decay factor is EXP(0) == 1, so a fresh spawn tie
    # (permanent LN(1+count)) and a fresh message tie (EXP(0)*LN(1+count)) coincide.
    _event(fake_loki, event_type="spawn", agent_id=b, target=a, days_ago=0.0)
    _event(fake_loki, event_type="send_message", agent_id=c, target=a, days_ago=0.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    by_id = {r["agent_id"]: r for r in rows}  # pyright: ignore[reportUnknownVariableType]
    assert by_id[b]["score"] == by_id[c]["score"]


def test_lineage_permanent_message_decays_over_time(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    a = _seed_agent(db_conn)
    lineage = _seed_agent(db_conn)
    msg = _seed_agent(db_conn)
    # Both ties are 5 days old with the same count. The lineage (spawn) weight
    # does not decay; the message weight does -> lineage now outranks the message.
    _event(fake_loki, event_type="spawn", agent_id=lineage, target=a, days_ago=5.0)
    _event(fake_loki, event_type="send_message", agent_id=msg, target=a, days_ago=5.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    by_id = {r["agent_id"]: r for r in rows}  # pyright: ignore[reportUnknownVariableType]
    assert set(by_id) == {lineage, msg}  # pyright: ignore[reportUnknownArgumentType]
    assert by_id[lineage]["score"] > by_id[msg]["score"]


def test_resurrect_counts_as_a_tie(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _event(fake_loki, event_type="resurrect", agent_id=b, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    assert {r["agent_id"] for r in rows} == {b}  # pyright: ignore[reportUnknownVariableType]


def test_recency_decay_ranks_recent_first(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    a = _seed_agent(db_conn)
    recent = _seed_agent(db_conn)
    stale = _seed_agent(db_conn)
    _event(fake_loki, event_type="send_message", agent_id=recent, target=a, days_ago=0.0)
    _event(fake_loki, event_type="send_message", agent_id=stale, target=a, days_ago=10.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    assert [r["agent_id"] for r in rows] == [recent, stale]  # pyright: ignore[reportUnknownVariableType]
    assert rows[0]["score"] > rows[1]["score"]


def test_depth_limits_reach_and_gamma_decays_deeper_hops(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # Chain a - b - c, identical fresh edges. c is two hops from a.
    _event(fake_loki, event_type="send_message", agent_id=b, target=a, days_ago=0.0)
    _event(fake_loki, event_type="send_message", agent_id=c, target=b, days_ago=0.0)

    with TestClient(app) as client:
        depth1 = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]
        depth2 = _neighbors(client, a, depth=2)  # pyright: ignore[reportUnknownVariableType]

    # depth=1 sees only the direct neighbor b.
    assert {r["agent_id"] for r in depth1} == {b}  # pyright: ignore[reportUnknownVariableType]
    # depth=2 reaches c, marked as a 2-hop neighbor, and below b (gamma discount).
    by_id = {r["agent_id"]: r for r in depth2}  # pyright: ignore[reportUnknownVariableType]
    assert set(by_id) == {b, c}  # pyright: ignore[reportUnknownArgumentType]
    assert by_id[b]["depth"] == 1
    assert by_id[c]["depth"] == 2
    assert by_id[c]["score"] < by_id[b]["score"]


def test_terminated_neighbor_included_with_status(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    a = _seed_agent(db_conn)
    dead = _seed_agent(db_conn, status="terminated")
    _event(fake_loki, event_type="send_message", agent_id=dead, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    assert len(rows) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert rows[0]["agent_id"] == dead
    assert rows[0]["status"] == "terminated"


def test_limit_caps_result_count(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    a = _seed_agent(db_conn)
    for _ in range(5):
        peer = _seed_agent(db_conn)
        _event(fake_loki, event_type="send_message", agent_id=peer, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1, limit=2)  # pyright: ignore[reportUnknownVariableType]

    assert len(rows) == 2  # pyright: ignore[reportUnknownArgumentType]


def test_no_ties_returns_empty(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]
    assert rows == []


def test_unknown_agent_404(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents/999999/neighbors")
    assert resp.status_code == 404


def test_depth_out_of_range_422(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        assert client.get(f"/api/agents/{a}/neighbors", params={"depth": 0}).status_code == 422
        assert client.get(f"/api/agents/{a}/neighbors", params={"depth": 6}).status_code == 422


def test_telemetry_message_does_not_create_neighbor(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """The neighbor traversal reads category='audit' only: a send_message row
    written with category='telemetry' (a mislabeled write) must not produce a
    tie — the graph edge family is audit-only by contract."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    fake_loki.add(
        event="send_message",
        agent_id=b,
        target_agent_id=a,
        category="telemetry",
        ts_offset_hours=24.0,
    )

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]
    assert rows == []


def test_archive_and_loki_merge_on_the_same_pair(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """The two sides partition the timeline at the freeze boundary and sum
    exactly like one table: counts add before the LN, so one archive message
    + one live message weigh LN(1+2) — not 2 * LN(2)."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _archive_boundary_anchor(db_conn)
    _archive_event(db_conn, event_type="send_message", agent_id=b, target=a, age_hours=1.0)
    _event(fake_loki, event_type="send_message", agent_id=b, target=a, days_ago=0.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    assert len(rows) == 1  # pyright: ignore[reportUnknownArgumentType]
    score = rows[0]["score"]  # pyright: ignore[reportUnknownVariableType]
    assert score == pytest.approx(math.log1p(2), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


def test_archive_lineage_alone_forms_a_tie(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """A pre-cutover lineage tie lives in the frozen archive only — the walk
    must still reach it (the archive read is deliberate, not dropped)."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _archive_boundary_anchor(db_conn)
    _archive_event(db_conn, event_type="spawn", agent_id=b, target=a, age_hours=2.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    assert {r["agent_id"] for r in rows} == {b}  # pyright: ignore[reportUnknownVariableType]
    assert rows[0]["score"] == pytest.approx(math.log1p(1), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


# ── ancestors: the directed spawn/fork chain above the queried agent ──────


def _ancestors(client: TestClient, agent_id: int, **params: int) -> list[dict]:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    resp = client.get(f"/api/agents/{agent_id}/neighbors", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["ancestors"]


def test_ancestors_spawn_chain_nearest_first_walks_to_top(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """a spawns b, b spawns c: c's ancestors are [b, a] — nearest first, walked
    to the top. The event direction is the child's row: agent_id = the NEW
    agent, target_agent_id = its spawner, so b's row points at a."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    _event(fake_loki, event_type="spawn", agent_id=b, target=a)
    _event(fake_loki, event_type="spawn", agent_id=c, target=b)

    with TestClient(app) as client:
        rows = _ancestors(client, c)  # pyright: ignore[reportUnknownVariableType]
        # the parent is not an ancestor of itself — direction is read correctly
        rows_b = _ancestors(client, b)  # pyright: ignore[reportUnknownVariableType]

    assert [r["agent_id"] for r in rows] == [b, a]  # pyright: ignore[reportUnknownVariableType]
    assert [r["depth"] for r in rows] == [1, 2]  # pyright: ignore[reportUnknownVariableType]
    # each hop's edge is the permanent lineage weight, gamma-discounted per hop
    assert rows[0]["score"] == pytest.approx(math.log1p(1), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]
    assert rows[1]["score"] < rows[0]["score"]  # pyright: ignore[reportUnknownMemberType]
    assert [r["agent_id"] for r in rows_b] == [a]  # pyright: ignore[reportUnknownVariableType]


def test_ancestors_ignore_neighbor_depth_param(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """`depth` bounds the neighbor walk only; the ancestor chain always walks
    to the top — responsibility attribution needs the whole chain."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    _event(fake_loki, event_type="spawn", agent_id=b, target=a)
    _event(fake_loki, event_type="spawn", agent_id=c, target=b)

    with TestClient(app) as client:
        rows = _ancestors(client, c, depth=1)  # pyright: ignore[reportUnknownVariableType]

    assert [r["agent_id"] for r in rows] == [b, a]  # pyright: ignore[reportUnknownVariableType]


def test_ancestors_fork_forms_a_parent(db_conn: psycopg.Connection, fake_loki: FakeLoki) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _event(fake_loki, event_type="fork", agent_id=b, target=a)

    with TestClient(app) as client:
        rows = _ancestors(client, b)  # pyright: ignore[reportUnknownVariableType]

    assert [r["agent_id"] for r in rows] == [a]  # pyright: ignore[reportUnknownVariableType]


def test_ancestors_message_ties_and_resurrect_never_parent(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """Only creation events carry parentage: a message is a peer tie and a
    resurrect wakes an existing agent — neither makes the other an ancestor."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _event(fake_loki, event_type="send_message", agent_id=b, target=a)
    _event(fake_loki, event_type="resurrect", agent_id=b, target=a)

    with TestClient(app) as client:
        assert _ancestors(client, a) == []


def test_ancestors_no_spawner_returns_empty(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        assert _ancestors(client, a) == []


def test_ancestors_terminated_ancestor_included_with_status(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """A terminated parent stays in the chain (same inclusion rule as
    neighbors) and carries its status."""
    a = _seed_agent(db_conn, status="terminated")
    b = _seed_agent(db_conn)
    _event(fake_loki, event_type="spawn", agent_id=b, target=a)

    with TestClient(app) as client:
        rows = _ancestors(client, b)  # pyright: ignore[reportUnknownVariableType]

    assert len(rows) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert rows[0]["agent_id"] == a
    assert rows[0]["status"] == "terminated"


def test_ancestors_archive_lineage_read_directionally(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """A pre-cutover spawn row lives in the frozen archive only. The ancestor
    walk reads it directionally (child = agent_id, parent = target) — the
    neighbor merge's LEAST/GREATEST grouping deliberately discards that."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _archive_boundary_anchor(db_conn)
    _archive_event(db_conn, event_type="spawn", agent_id=b, target=a, age_hours=2.0)

    with TestClient(app) as client:
        rows = _ancestors(client, b)  # pyright: ignore[reportUnknownVariableType]

    assert [r["agent_id"] for r in rows] == [a]  # pyright: ignore[reportUnknownVariableType]
    assert rows[0]["score"] == pytest.approx(math.log1p(1), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


def test_ancestors_archive_and_loki_merge_counts(
    db_conn: psycopg.Connection, fake_loki: FakeLoki
) -> None:
    """One archive spawn + one live spawn on the same directed pair weigh
    LN(1+2), exactly like the neighbor merge (counts add before the LN)."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _archive_boundary_anchor(db_conn)
    _archive_event(db_conn, event_type="spawn", agent_id=b, target=a, age_hours=1.0)
    _event(fake_loki, event_type="spawn", agent_id=b, target=a, days_ago=0.0)

    with TestClient(app) as client:
        rows = _ancestors(client, b)  # pyright: ignore[reportUnknownVariableType]

    assert [r["agent_id"] for r in rows] == [a]  # pyright: ignore[reportUnknownVariableType]
    assert rows[0]["score"] == pytest.approx(math.log1p(2), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


# ── migration round-trip: the down migration must actually execute ────────
