"""GET /api/agents/{id}/shell/{sid} HTTP integration tests.

Lock down the shell monitor page's capture contract — the endpoint dispatches
a `shell_capture` op to the machine the agent runs on (one uniform path for
every machine, the gateway's own box included; no local session probing in the
router). Covers the tri-state: unknown agent 404, op failure 404 (no such
shell / capture failed), op unreachable 503 (machine down), and the success
shape + `?lines=` forwarding. The runner-side capture itself (name
reconstruction, capture-pane invocation, error mapping) is covered by
`tests/gateway/test_cluster_status_fields.py` (capture_shell unit tests).
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from gateway.routers import shell as shell_router
from ops import cluster_rpc


def _insert_agent(db: psycopg.Connection, *, machine: str = "unknown") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES ('t') RETURNING id", ())
        row = cur.fetchone()
        assert row is not None, "INSERT ... RETURNING must return a row"
        aid = row[0]
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, machine) "
            "VALUES (%s, 'user', 'running', %s)",
            (aid, machine),
        )
    return aid


async def _ok_dispatch(
    target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
) -> dict[str, object]:
    """A reachable runner answering shell_capture — echoes the request fields
    back so the test can assert what was forwarded."""
    return {
        "session_name": f"ava-agent-{payload['agent_id']}-shell-3-watcher",
        "lines": ["line one", "line two"],
    }


def test_shell_unknown_agent_404(db_conn: psycopg.Connection) -> None:
    """No agents_meta row → 404 (fail-fast, before any dispatch)."""
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/agents/999999/shell/0")
    assert resp.status_code == 404


def test_shell_no_such_session_404(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner ran the op and reported failure (no live shell with that id —
    capture_shell's ShellNotFoundError surfaces as a failed op) → 404, same as a
    local miss."""
    aid = _insert_agent(db_conn)
    db_conn.commit()

    async def _failed_dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise cluster_rpc.ClusterOpFailed(
            {"error": "ShellNotFoundError: agent 1 has no live shell 0 on this host"}
        )

    monkeypatch.setattr(shell_router._cluster_rpc, "dispatch_to_machine", _failed_dispatch)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/shell/0")
    assert resp.status_code == 404
    assert "capture failed" in resp.json()["detail"]


def test_shell_capture_success(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live shell on the agent's machine → 200, response carries the runner's
    session_name + lines; the op is dispatched to the agent's machine with the
    default ?lines=200."""
    aid = _insert_agent(db_conn, machine="wsl")
    db_conn.commit()

    seen: dict[str, object] = {}

    async def _capture_dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        seen["machine"] = target_machine
        seen["kind"] = kind
        seen["payload"] = payload
        return await _ok_dispatch(target_machine, kind, payload)

    monkeypatch.setattr(shell_router._cluster_rpc, "dispatch_to_machine", _capture_dispatch)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/shell/3")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "agent_id": aid,
        "session_id": 3,
        "session_name": f"ava-agent-{aid}-shell-3-watcher",
        "lines": ["line one", "line two"],
    }
    # Uniform path: dispatched to the agent's machine (a remote runner here),
    # never probed locally.
    assert seen["machine"] == "wsl"
    assert seen["kind"] == "shell_capture"
    assert seen["payload"] == {"agent_id": aid, "session_id": 3, "lines": 200}


def test_shell_capture_custom_lines_forwarded(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/shell/1?lines=500 → the op payload carries lines=500."""
    aid = _insert_agent(db_conn)
    db_conn.commit()

    seen: dict[str, object] = {}

    async def _capture_dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        seen["payload"] = payload
        return await _ok_dispatch(target_machine, kind, payload)

    monkeypatch.setattr(shell_router._cluster_rpc, "dispatch_to_machine", _capture_dispatch)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/shell/1?lines=500")
    assert resp.status_code == 200
    assert seen["payload"] == {"agent_id": aid, "session_id": 1, "lines": 500}


def test_shell_machine_unreachable_503(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's machine's ops server is unreachable → 503 (the shell may
    still exist; the gateway just cannot reach it right now)."""
    aid = _insert_agent(db_conn, machine="wsl")
    db_conn.commit()

    async def _unreachable_dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise cluster_rpc.ClusterOpUnreachable("connect failed")

    monkeypatch.setattr(shell_router._cluster_rpc, "dispatch_to_machine", _unreachable_dispatch)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/shell/1")
    assert resp.status_code == 503
    assert "unreachable" in resp.json()["detail"]
