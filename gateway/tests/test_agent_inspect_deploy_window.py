"""(#4986) The inspector's shell probe reads the deploy window per request."""

from __future__ import annotations

import logging

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.inspect import router as inspect_router
from ops import cluster_rpc
from ops.deploy_window import DeployWindow


def _insert_agent(db: psycopg.Connection) -> int:
    """One running agent row — the inspect route only needs the id to exist."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES ('probe') RETURNING id")
        row = cur.fetchone()
        assert row is not None, "INSERT ... RETURNING must return one row"
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (row[0],),
        )
    return row[0]


def test_probe_failure_severity_follows_the_deploy_window(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A machine mid-maintenance is expected-unreachable: the observation stays
    unavailable, the report drops from WARNING to INFO carrying the window's
    detail, and the RPC dispatch is quieted — all read per request, so the
    severity follows a flip between two calls (task #4986)."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    seen: list[dict[str, object]] = []

    async def probe(*args: object, **kwargs: object) -> dict[str, object]:
        seen.append(kwargs)
        raise cluster_rpc.ClusterOpUnreachable("connect failed")

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", probe)
    window = DeployWindow(active=False, detail="no deploy in flight")

    def _window(_db: object) -> DeployWindow:
        return window

    monkeypatch.setattr(inspect_router, "deploy_in_flight", _window)
    caplog.set_level(logging.INFO, logger="gateway.inspect.router")

    with TestClient(app) as client:
        idle = client.get(f"/api/agents/{aid}/inspect/live")
        window = DeployWindow(active=True, detail="machine 'wsl' is mid-deploy")
        during = client.get(f"/api/agents/{aid}/inspect/live")

    assert idle.status_code == 200
    assert during.status_code == 200
    assert idle.json()["shells_available"] is False
    assert during.json()["shells_available"] is False
    assert [kwargs["quiet_unreachable"] for kwargs in seen] == [False, True]
    records = [r for r in caplog.records if "shell observation unavailable" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.WARNING, logging.INFO]
    assert "mid-deploy" in records[1].getMessage()
