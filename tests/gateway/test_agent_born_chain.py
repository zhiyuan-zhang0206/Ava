"""GET /api/agents/{id}/born-chain integration tests.

The light half of /neighbors, split out for the inherited-memory context note:
one born_spawner walk (no tie graph, no Loki) plus label / machine / status per
ancestor. Pins the shape the note's locality gate depends on: `machine` is
carried per ancestor, terminated ancestors stay in the chain, and a non-agent
spawner stops it.
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi.testclient import TestClient

from gateway.app import app


def _seed_agent(
    db_conn: psycopg.Connection,
    *,
    born_spawner: str | None = None,
    machine: str | None = None,
    label: str | None = None,
    status: str = "running",
) -> int:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
        assert row is not None
        new_id = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, born_spawner, status, machine) "
            "VALUES (%s, 'test', %s, %s, %s)",
            (new_id, born_spawner, status, machine),
        )
    db_conn.commit()
    return new_id


def _born_chain(client: TestClient, agent_id: int) -> list[dict[str, Any]]:
    resp = client.get(f"/api/agents/{agent_id}/born-chain")
    assert resp.status_code == 200, resp.text
    return resp.json()["ancestors"]


def test_born_chain_walks_nearest_first_with_machine_label_status(
    db_conn: psycopg.Connection,
) -> None:
    grandparent = _seed_agent(db_conn, machine="host-a", label="Grandparent", status="terminated")
    parent = _seed_agent(
        db_conn, born_spawner=f"agent:{grandparent}", machine="host-b", label="Parent"
    )
    child = _seed_agent(db_conn, born_spawner=f"agent:{parent}", machine="host-b")

    with TestClient(app) as client:
        rows = _born_chain(client, child)

    assert [r["agent_id"] for r in rows] == [parent, grandparent]
    assert [r["depth"] for r in rows] == [1, 2]
    assert rows[0]["label"] == "Parent"
    assert rows[0]["machine"] == "host-b"
    assert rows[0]["status"] == "running"
    # Terminated ancestors stay in the immutable chain, machine included.
    assert rows[1]["label"] == "Grandparent"
    assert rows[1]["machine"] == "host-a"
    assert rows[1]["status"] == "terminated"


def test_born_chain_stops_at_a_non_agent_spawner(db_conn: psycopg.Connection) -> None:
    """A user-born agent is a valid PARENT (its child's chain has one row);
    the walk stops one hop above it, where born_spawner stops being `agent:N`."""
    user_spawned = _seed_agent(db_conn, born_spawner="user", machine="host-a")
    child = _seed_agent(db_conn, born_spawner=f"agent:{user_spawned}", machine="host-a")

    with TestClient(app) as client:
        rows = _born_chain(client, child)
        assert [r["agent_id"] for r in rows] == [user_spawned]
        assert _born_chain(client, user_spawned) == []


def test_born_chain_unknown_agent_404(db_conn: psycopg.Connection) -> None:
    with TestClient(app) as client:
        resp = client.get("/api/agents/99999999/born-chain")
    assert resp.status_code == 404
