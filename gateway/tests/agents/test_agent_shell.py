from __future__ import annotations

import psycopg
import pytest

from ops.cluster import rpc as cluster_rpc
from tests.fixtures.gateway_config import gateway_test_client


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
    _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
) -> dict[str, object]:
    return {
        "session_name": f"ava-agent-{payload['agent_id']}-shell-3-watcher",
        "lines": ["line one", "line two"],
    }


def test_shell_unknown_agent_404(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with gateway_test_client() as client:
        resp = client.get("/api/agents/999999/shell/0")
    assert resp.status_code == 404


def test_shell_no_such_session_404(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()

    async def _failed_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise cluster_rpc.ClusterOpFailed(
            {"error": "ShellNotFoundError: agent 1 has no live shell 0 on this host"}
        )

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _failed_dispatch)

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{aid}/shell/0")
    assert resp.status_code == 404
    assert "capture failed" in resp.json()["detail"]


def test_shell_capture_success(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    db_conn.commit()

    seen: dict[str, object] = {}

    async def _capture_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        seen["machine"] = target_machine
        seen["kind"] = kind
        seen["payload"] = payload
        return await _ok_dispatch(_db, target_machine, kind, payload)

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _capture_dispatch)

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{aid}/shell/3")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "agent_id": aid,
        "session_id": 3,
        "session_name": f"ava-agent-{aid}-shell-3-watcher",
        "lines": ["line one", "line two"],
        "created_at": None,
        "uptime_seconds": 0,
        "expires_at": None,
        "renewals": 0,
        "last_renewed_at": None,
    }
    assert seen["machine"] == "runner-a"
    assert seen["kind"] == "shell_capture"
    assert seen["payload"] == {"agent_id": aid, "session_id": 3, "lines": 200}


def test_shell_capture_custom_lines_forwarded(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()

    seen: dict[str, object] = {}

    async def _capture_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        seen["payload"] = payload
        return await _ok_dispatch(_db, target_machine, kind, payload)

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _capture_dispatch)

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{aid}/shell/1?lines=500")
    assert resp.status_code == 200
    assert seen["payload"] == {"agent_id": aid, "session_id": 1, "lines": 500}


def test_shell_capture_default_lines_follow_display_config(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.config import settings

    monkeypatch.setattr(settings.display, "shell_capture_default_lines", 137)
    aid = _insert_agent(db_conn)
    db_conn.commit()

    seen: dict[str, object] = {}

    async def _capture_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        seen["payload"] = payload
        return await _ok_dispatch(_db, target_machine, kind, payload)

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _capture_dispatch)

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{aid}/shell/1")
    assert resp.status_code == 200
    assert seen["payload"] == {"agent_id": aid, "session_id": 1, "lines": 137}


def test_shell_capture_carries_created_at_and_ttl_deadline(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    aid = _insert_agent(db_conn, machine="runner-a")
    launched = datetime.now(tz=UTC) - timedelta(minutes=30)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) VALUES (%s, %s, %s)",
            (aid, 3, datetime.now(tz=UTC) + timedelta(hours=2)),
        )
    db_conn.commit()

    async def _meta_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return {
            "session_name": f"ava-agent-{aid}-shell-3-dev",
            "lines": ["line one"],
            "created_at": launched.isoformat(),
            "uptime_seconds": 1800,
        }

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _meta_dispatch)

    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/shell/3").json()
    assert body["created_at"] == launched.astimezone(UTC).isoformat().replace("+00:00", "Z")
    assert body["uptime_seconds"] == 1800
    assert body["expires_at"] is not None
    assert body["renewals"] == 0
    assert body["last_renewed_at"] is None


@pytest.mark.parametrize("name", ["page-preview", "schedule-check"])
def test_shell_capture_without_row_has_no_deadline(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from datetime import UTC, datetime, timedelta

    aid = _insert_agent(db_conn, machine="runner-a")
    launched = datetime.now(tz=UTC) - timedelta(hours=3)
    db_conn.commit()

    async def _meta_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return {
            "session_name": f"ava-agent-{aid}-shell-7-{name}",
            "lines": ["hello"],
            "created_at": launched.isoformat(),
            "uptime_seconds": 10800,
        }

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _meta_dispatch)

    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/shell/7").json()
    assert datetime.fromisoformat(body["created_at"]) == launched
    assert body["expires_at"] is None
    assert body["renewals"] == 0
    assert body["last_renewed_at"] is None


def test_shell_capture_without_row_and_epoch_keeps_no_deadline(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    db_conn.commit()

    async def _meta_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return {
            "session_name": f"ava-agent-{aid}-shell-8",
            "lines": ["hello"],
            "created_at": None,
            "uptime_seconds": 0,
        }

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _meta_dispatch)

    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/shell/8").json()
    assert body["expires_at"] is None


def test_shell_capture_returns_recorded_deadline(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    aid = _insert_agent(db_conn, machine="runner-a")
    launched = datetime.now(tz=UTC) - timedelta(minutes=5)
    deadline = datetime.now(tz=UTC) + timedelta(minutes=30)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) VALUES (%s, %s, %s)",
            (aid, 9, deadline),
        )
    db_conn.commit()

    async def _meta_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return {
            "session_name": f"ava-agent-{aid}-shell-9",
            "lines": ["hello"],
            "created_at": launched.isoformat(),
            "uptime_seconds": 300,
        }

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _meta_dispatch)

    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/shell/9").json()
    assert datetime.fromisoformat(body["expires_at"]) == deadline


def test_shell_machine_unreachable_503(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn, machine="runner-a")
    db_conn.commit()

    async def _unreachable_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise cluster_rpc.ClusterOpUnreachable("connect failed")

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable_dispatch)

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{aid}/shell/1")
    assert resp.status_code == 503
    assert "unreachable" in resp.json()["detail"]


def test_shell_capture_carries_renewal_facts(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    aid = _insert_agent(db_conn, machine="runner-a")
    renewed = datetime.now(tz=UTC) - timedelta(minutes=5)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, renewals, "
            "last_renewed_at) VALUES (%s, %s, %s, %s, %s)",
            (aid, 4, datetime.now(tz=UTC) + timedelta(hours=2), 3, renewed),
        )
    db_conn.commit()
    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _ok_dispatch)

    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/shell/4").json()
    assert body["renewals"] == 3
    assert body["last_renewed_at"] == renewed.astimezone(UTC).isoformat().replace("+00:00", "Z")
