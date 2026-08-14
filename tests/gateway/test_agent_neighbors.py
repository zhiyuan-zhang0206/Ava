"""GET /api/agents/{id}/neighbors integration tests.

FastAPI TestClient + real ava_test DB. Exercises the `agent_neighbors` SQL
function (recursive walk over the events table, category=audit) end to end: undirected ties, permanent
lineage weights (spawn/fork/resurrect, no time decay) vs decaying message weights
(send_message, EXP(-k*days)), per-hop gamma decay, terminated inclusion, limit,
and the self/root exclusions. This is the one place a wrong LEAST/GREATEST, decay
term, or join breaks live but passes the frontend mock tests.
"""

import psycopg
from fastapi.testclient import TestClient

from gateway.app import app


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
    db_conn: psycopg.Connection,
    *,
    event_type: str,
    agent_id: int,
    target: int | None,
    days_ago: float = 0.0,
    count: int = 1,
) -> None:
    """Insert `count` audit events rows for an (agent_id, target) pair at a fixed age.

    agent_id and target are the two endpoints of the inter-agent tie; the
    `agent_neighbors` function keys purely on them (the source string is
    irrelevant to the graph), so a plain INSERT with an explicit ts is
    enough to drive the recency term deterministically.
    """
    with db_conn.cursor() as cur:
        for _ in range(count):
            cur.execute(
                "INSERT INTO events "
                "(ts, agent_id, event_name, source, target_agent_id, machine, process, category, level) "
                "VALUES (now() - %s * interval '1 day', %s, %s, 'test', %s, "
                "'test', 'test', 'audit', 'info')",
                (days_ago, agent_id, event_type, target),
            )
    db_conn.commit()


def _neighbors(client: TestClient, agent_id: int, **params: int) -> list[dict]:  # pyright: ignore[reportMissingTypeArgument, reportUnknownParameterType]
    resp = client.get(f"/api/agents/{agent_id}/neighbors", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["neighbors"]


def test_direct_ties_both_directions_self_and_root_excluded(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # b messaged a (tie a-b); a spawned c (tie a-c). Direction does not matter.
    _event(db_conn, event_type="send_message", agent_id=b, target=a)
    _event(db_conn, event_type="spawn", agent_id=c, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    ids = {r["agent_id"] for r in rows}  # pyright: ignore[reportUnknownVariableType]
    assert ids == {b, c}  # root a excluded; both neighbors found regardless of direction
    assert all(r["depth"] == 1 for r in rows)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]


def test_lineage_and_message_equal_at_zero_age(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # At age 0 the message decay factor is EXP(0) == 1, so a fresh spawn tie
    # (permanent LN(1+count)) and a fresh message tie (EXP(0)*LN(1+count)) coincide.
    # No per-type multiplier here (the FleetView graph's 2x lineage multiplier is
    # deliberately not applied to neighbor scores). They diverge only over time.
    _event(db_conn, event_type="spawn", agent_id=b, target=a, days_ago=0.0, count=1)
    _event(db_conn, event_type="send_message", agent_id=c, target=a, days_ago=0.0, count=1)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    by_id = {r["agent_id"]: r for r in rows}  # pyright: ignore[reportUnknownVariableType]
    assert by_id[b]["score"] == by_id[c]["score"]


def test_lineage_permanent_message_decays_over_time(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    lineage = _seed_agent(db_conn)
    msg = _seed_agent(db_conn)
    # Both ties are 5 days old with the same count. The lineage (spawn) weight does
    # not decay; the message weight does -> lineage now outranks the message, and
    # the message can even fade below a stale lineage tie.
    _event(db_conn, event_type="spawn", agent_id=lineage, target=a, days_ago=5.0)
    _event(db_conn, event_type="send_message", agent_id=msg, target=a, days_ago=5.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    by_id = {r["agent_id"]: r for r in rows}  # pyright: ignore[reportUnknownVariableType]
    assert set(by_id) == {lineage, msg}  # pyright: ignore[reportUnknownArgumentType]
    assert by_id[lineage]["score"] > by_id[msg]["score"]


def test_resurrect_counts_as_a_tie(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _event(db_conn, event_type="resurrect", agent_id=b, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    assert {r["agent_id"] for r in rows} == {b}  # pyright: ignore[reportUnknownVariableType]


def test_recency_decay_ranks_recent_first(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    recent = _seed_agent(db_conn)
    stale = _seed_agent(db_conn)
    _event(db_conn, event_type="send_message", agent_id=recent, target=a, days_ago=0.0)
    _event(db_conn, event_type="send_message", agent_id=stale, target=a, days_ago=10.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    assert [r["agent_id"] for r in rows] == [recent, stale]  # pyright: ignore[reportUnknownVariableType]
    assert rows[0]["score"] > rows[1]["score"]


def test_depth_limits_reach_and_gamma_decays_deeper_hops(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # Chain a - b - c, identical fresh edges. c is two hops from a.
    _event(db_conn, event_type="send_message", agent_id=b, target=a, days_ago=0.0)
    _event(db_conn, event_type="send_message", agent_id=c, target=b, days_ago=0.0)

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


def test_terminated_neighbor_included_with_status(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    dead = _seed_agent(db_conn, status="terminated")
    _event(db_conn, event_type="send_message", agent_id=dead, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]

    assert len(rows) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert rows[0]["agent_id"] == dead
    assert rows[0]["status"] == "terminated"


def test_limit_caps_result_count(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    for _ in range(5):
        peer = _seed_agent(db_conn)
        _event(db_conn, event_type="send_message", agent_id=peer, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1, limit=2)  # pyright: ignore[reportUnknownVariableType]

    assert len(rows) == 2  # pyright: ignore[reportUnknownArgumentType]


def test_no_ties_returns_empty(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]
    assert rows == []


def test_unknown_agent_404(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents/999999/neighbors")
    assert resp.status_code == 404


def test_depth_out_of_range_422(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        assert client.get(f"/api/agents/{a}/neighbors", params={"depth": 0}).status_code == 422
        assert client.get(f"/api/agents/{a}/neighbors", params={"depth": 6}).status_code == 422


def test_telemetry_message_does_not_create_neighbor(db_conn: psycopg.Connection) -> None:
    """The neighbor traversal reads category='audit' only: a send_message row
    written with category='telemetry' (a mislabeled write) must not produce a
    tie — the graph edge family is audit-only by contract."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events "
            "(ts, agent_id, event_name, source, target_agent_id, machine, process, category, level) "
            "VALUES (now() - interval '1 day', %s, 'send_message', 'test', %s, "
            "'test', 'test', 'telemetry', 'info')",
            (b, a),
        )
    db_conn.commit()

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)  # pyright: ignore[reportUnknownVariableType]
    assert rows == []


# ── migration round-trip: the W9 down migration must actually execute ────────


