"""GET /api/agents/{id}/neighbors integration tests.

FastAPI TestClient + real ava_test DB. The tie graph is aggregated from the audit
record (`audit_events`) and the walk runs in Python (gateway/inspect/neighbors.py).
Covered end to end: undirected ties, permanent lineage weights
(spawn/fork/resurrect, no time decay) vs decaying message weights
(send_message, EXP(-k*days)), per-hop gamma decay, terminated inclusion, limit,
self/root exclusions, and the birth chain.
"""

import math
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app


def _seed_agent(
    db_conn: psycopg.Connection,
    *,
    status: str = "running",
    born_spawner: str | None = None,
) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, born_spawner, status) VALUES (%s, 'test', %s, %s)",
            (new_id, born_spawner, status),
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
    """Record `count` audit events for an (agent_id, target) pair at a fixed age.
    agent_id and target are the two endpoints of the inter-agent tie; the walk
    keys purely on them, so the source string is irrelevant to the graph."""
    for _ in range(count):
        db_conn.execute(
            "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, "
            "source, agent_id, target_agent_id) "
            "VALUES (%s, now() - (%s * interval '1 day'), 'test', 'test', %s, 'info', 'test', "
            "%s, %s)",
            (uuid.uuid4().int % (1 << 62), days_ago, event_type, agent_id, target),
        )
    db_conn.commit()


def _neighbors(client: TestClient, agent_id: int, **params: int) -> list[dict]:
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
        rows = _neighbors(client, a, depth=1)

    ids = {r["agent_id"] for r in rows}
    assert ids == {b, c}  # root a excluded; both neighbors found regardless of direction
    assert all(r["depth"] == 1 for r in rows)  # pyright: ignore[reportUnknownArgumentType]


def test_lineage_and_message_equal_at_zero_age(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # At age 0 the message decay factor is EXP(0) == 1, so a fresh spawn tie
    # (permanent LN(1+count)) and a fresh message tie (EXP(0)*LN(1+count)) coincide.
    _event(db_conn, event_type="spawn", agent_id=b, target=a, days_ago=0.0)
    _event(db_conn, event_type="send_message", agent_id=c, target=a, days_ago=0.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    by_id = {r["agent_id"]: r for r in rows}
    assert by_id[b]["score"] == by_id[c]["score"]


def test_lineage_permanent_message_decays_over_time(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    lineage = _seed_agent(db_conn)
    msg = _seed_agent(db_conn)
    # Both ties are 5 days old with the same count. The lineage (spawn) weight
    # does not decay; the message weight does -> lineage now outranks the message.
    _event(db_conn, event_type="spawn", agent_id=lineage, target=a, days_ago=5.0)
    _event(db_conn, event_type="send_message", agent_id=msg, target=a, days_ago=5.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    by_id = {r["agent_id"]: r for r in rows}
    assert set(by_id) == {lineage, msg}  # pyright: ignore[reportUnknownArgumentType]
    assert by_id[lineage]["score"] > by_id[msg]["score"]


def test_resurrect_counts_as_a_tie(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _event(db_conn, event_type="resurrect", agent_id=b, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    assert {r["agent_id"] for r in rows} == {b}


def test_recency_decay_ranks_recent_first(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    recent = _seed_agent(db_conn)
    stale = _seed_agent(db_conn)
    _event(db_conn, event_type="send_message", agent_id=recent, target=a, days_ago=0.0)
    _event(db_conn, event_type="send_message", agent_id=stale, target=a, days_ago=10.0)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    assert [r["agent_id"] for r in rows] == [recent, stale]
    assert rows[0]["score"] > rows[1]["score"]


def test_depth_limits_reach_and_gamma_decays_deeper_hops(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    c = _seed_agent(db_conn)
    # Chain a - b - c, identical fresh edges. c is two hops from a.
    _event(db_conn, event_type="send_message", agent_id=b, target=a, days_ago=0.0)
    _event(db_conn, event_type="send_message", agent_id=c, target=b, days_ago=0.0)

    with TestClient(app) as client:
        depth1 = _neighbors(client, a, depth=1)
        depth2 = _neighbors(client, a, depth=2)

    # depth=1 sees only the direct neighbor b.
    assert {r["agent_id"] for r in depth1} == {b}
    # depth=2 reaches c, marked as a 2-hop neighbor, and below b (gamma discount).
    by_id = {r["agent_id"]: r for r in depth2}
    assert set(by_id) == {b, c}  # pyright: ignore[reportUnknownArgumentType]
    assert by_id[b]["depth"] == 1
    assert by_id[c]["depth"] == 2
    assert by_id[c]["score"] < by_id[b]["score"]


def test_terminated_neighbor_included_with_status(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    dead = _seed_agent(db_conn, status="terminated")
    _event(db_conn, event_type="send_message", agent_id=dead, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)

    assert len(rows) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert rows[0]["agent_id"] == dead
    assert rows[0]["status"] == "terminated"


def test_limit_caps_result_count(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    for _ in range(5):
        peer = _seed_agent(db_conn)
        _event(db_conn, event_type="send_message", agent_id=peer, target=a)

    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1, limit=2)

    assert len(rows) == 2  # pyright: ignore[reportUnknownArgumentType]


def test_defaults_come_from_display_config(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitted depth/limit resolve from settings.display.neighbors_default_*
    (``AVA_NEIGHBORS_DEFAULT_DEPTH`` / ``AVA_NEIGHBORS_DEFAULT_LIMIT``); the
    literals 1/20 are only the fields' defaults, not hard-coded query
    parameters."""
    from base.config import settings

    a = _seed_agent(db_conn)
    for _ in range(3):
        peer = _seed_agent(db_conn)
        _event(db_conn, event_type="send_message", agent_id=peer, target=a)

    monkeypatch.setattr(settings.display, "neighbors_default_limit", 2)
    with TestClient(app) as client:
        rows = _neighbors(client, a)

    assert len(rows) == 2  # pyright: ignore[reportUnknownArgumentType]


def test_no_ties_returns_empty(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        rows = _neighbors(client, a, depth=1)
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


# ── ancestors: the immutable birth chain above the queried agent ─────────


def _ancestors(client: TestClient, agent_id: int, **params: int) -> list[dict]:
    resp = client.get(f"/api/agents/{agent_id}/neighbors", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["ancestors"]


def test_ancestors_spawn_chain_nearest_first_walks_to_top(db_conn: psycopg.Connection) -> None:
    """a births b, b births c: c's ancestors are [b, a], nearest first."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")
    c = _seed_agent(db_conn, born_spawner=f"agent:{b}")

    with TestClient(app) as client:
        rows = _ancestors(client, c)
        # the parent is not an ancestor of itself — the chain is read correctly
        rows_b = _ancestors(client, b)

    assert [r["agent_id"] for r in rows] == [b, a]
    assert [r["depth"] for r in rows] == [1, 2]
    # each hop's edge is the permanent lineage weight, gamma-discounted per hop
    assert rows[0]["score"] == pytest.approx(math.log1p(1), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]
    assert rows[1]["score"] < rows[0]["score"]
    assert [r["agent_id"] for r in rows_b] == [a]


def test_ancestors_ignore_neighbor_depth_param(db_conn: psycopg.Connection) -> None:
    """`depth` bounds the neighbor walk only; the ancestor chain always walks
    to the top — responsibility attribution needs the whole chain."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")
    c = _seed_agent(db_conn, born_spawner=f"agent:{b}")

    with TestClient(app) as client:
        rows = _ancestors(client, c, depth=1)

    assert [r["agent_id"] for r in rows] == [b, a]


def test_ancestors_fork_forms_a_parent(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")

    with TestClient(app) as client:
        rows = _ancestors(client, b)

    assert [r["agent_id"] for r in rows] == [a]


def test_ancestors_message_ties_and_resurrect_never_parent(db_conn: psycopg.Connection) -> None:
    """Message and resurrect ties do not form ancestors without born_spawner."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn)
    _event(db_conn, event_type="send_message", agent_id=b, target=a)
    _event(db_conn, event_type="resurrect", agent_id=b, target=a)

    with TestClient(app) as client:
        assert _ancestors(client, a) == []


def test_ancestors_no_spawner_returns_empty(db_conn: psycopg.Connection) -> None:
    a = _seed_agent(db_conn)
    with TestClient(app) as client:
        assert _ancestors(client, a) == []


def test_ancestors_terminated_ancestor_included_with_status(db_conn: psycopg.Connection) -> None:
    """A terminated parent stays in the chain (same inclusion rule as
    neighbors) and carries its status."""
    a = _seed_agent(db_conn, status="terminated")
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")

    with TestClient(app) as client:
        rows = _ancestors(client, b)

    assert len(rows) == 1  # pyright: ignore[reportUnknownArgumentType]
    assert rows[0]["agent_id"] == a
    assert rows[0]["status"] == "terminated"


def test_ancestors_read_born_spawner_not_the_recorded_events(db_conn: psycopg.Connection) -> None:
    """Ancestor lineage is independent of the recorded spawn events."""
    a = _seed_agent(db_conn)
    stale_event_parent = _seed_agent(db_conn)
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")
    _event(db_conn, event_type="spawn", agent_id=b, target=stale_event_parent)

    with TestClient(app) as client:
        rows = _ancestors(client, b)

    assert [r["agent_id"] for r in rows] == [a]
    assert rows[0]["score"] == pytest.approx(math.log1p(1), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


def test_ancestors_use_one_constant_weight_per_birth_edge(db_conn: psycopg.Connection) -> None:
    """born_spawner has one parent per child, independent of event counts."""
    a = _seed_agent(db_conn)
    b = _seed_agent(db_conn, born_spawner=f"agent:{a}")

    with TestClient(app) as client:
        rows = _ancestors(client, b)

    assert [r["agent_id"] for r in rows] == [a]
    assert rows[0]["score"] == pytest.approx(math.log1p(1), abs=1e-3)  # pyright: ignore[reportUnknownMemberType]


# ── frozen archive cache: the immutable Loki archive is read once a day ──
