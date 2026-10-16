from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from base.config import settings
from gateway.inspect import router as inspect_router
from ops.cluster import rpc as cluster_rpc
from services.wake.heartbeat import JITTER_SPAN_S, STALE_PENDING_S
from tests.fixtures.gateway_config import gateway_test_client


def _insert_agent_row(db: psycopg.Connection, label: str = "t") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None, "INSERT ... RETURNING must return one row"
    return row[0]


def _insert_agent(
    db: psycopg.Connection,
    *,
    status: str = "running",
    config_overlay: dict | None = None,
    status_changed_s_ago: float | None = None,
    paused_until_s_ahead: float | None = None,
) -> int:
    tid = _insert_agent_row(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, config_overlay) "
            "VALUES (%s, 'user', %s, %s::jsonb)",
            (tid, status, json.dumps(config_overlay) if config_overlay is not None else None),
        )
        if status_changed_s_ago is not None:
            cur.execute(
                "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => %s), "
                "       last_active_at = now() - make_interval(secs => %s) "
                "WHERE id = %s",
                (status_changed_s_ago, status_changed_s_ago, tid),
            )
        if paused_until_s_ahead is not None:
            cur.execute(
                "UPDATE agents_meta SET heartbeat_paused_until = now() + make_interval(secs => %s) "
                "WHERE id = %s",
                (paused_until_s_ahead, tid),
            )
    return tid


def _seconds_from_now(iso: str) -> float:
    return (datetime.fromisoformat(iso) - datetime.now(UTC)).total_seconds()


def _insert_pending_inbound(
    db: psycopg.Connection,
    *,
    agent_id: int,
    kind: str = "heartbeat",
    created_s_ago: float | None = None,
) -> None:
    with db.cursor() as cur:
        if created_s_ago is None:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind) VALUES (%s, %s, %s)",
                (agent_id, "Heartbeat.", kind),
            )
        else:
            cur.execute(
                "INSERT INTO inbound_messages (agent_id, content, kind, created_at) "
                "VALUES (%s, %s, %s, now() - make_interval(secs => %s))",
                (agent_id, "Heartbeat.", kind, created_s_ago),
            )


def test_inspect_unknown_agent_404(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with gateway_test_client() as client:
        resp = client.get("/api/agents/999999/inspect/statistics")
    assert resp.status_code == 404


def test_inspect_live_returns_only_window_independent_fields(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = _insert_agent(
        db_conn,
        status="idling",
        config_overlay={"llm_model": "claude-opus-4-8"},
    )
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'runner-a' WHERE id = %s", (aid,))
    _pause_row(db_conn, agent_id=aid, duration_s=1800, hours_ago=1)
    db_conn.commit()

    async def dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        assert (target_machine, kind, payload) == (
            "runner-a",
            "shell_probe",
            {"agent_id": aid},
        )
        return {
            "shells": [{"id": 5, "name": "live-shell", "created_at": None, "uptime_seconds": 42}]
        }

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", dispatch)
    with gateway_test_client() as client:
        response = client.get(f"/api/agents/{aid}/inspect/live")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "agent_id",
        "machine",
        "status",
        "liveness_state",
        "last_probe_at",
        "observation",
        "shells_available",
        "spawned_at",
        "started_at",
        "shells",
        "config_overlay",
        "notice",
        "heartbeat",
    }
    assert body["agent_id"] == aid
    assert body["machine"] == "runner-a"
    assert body["status"] == "idling"
    assert body["shells_available"] is True
    assert body["observation"]["runtime_owner"] == "unknown"
    assert body["config_overlay"] == {"llm_model": "claude-opus-4-8"}
    assert body["shells"] == [
        {
            "id": 5,
            "name": "live-shell",
            "created_at": None,
            "uptime_seconds": 42,
            "expires_at": None,
        }
    ]
    assert body["heartbeat"]["last_pause"]["duration_s"] == 1800
    assert {"cost", "stats", "tps", "activity"}.isdisjoint(body)


def test_inspect_live_unknown_agent_404(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with gateway_test_client() as client:
        response = client.get("/api/agents/999999/inspect/live")
    assert response.status_code == 404


def test_inspect_live_probe_failure_is_unavailable_not_empty_success(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()

    async def unreachable(*args: object, **kwargs: object) -> dict[str, object]:
        raise inspect_router.cluster_rpc.ClusterOpUnreachable("connect failed")

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", unreachable)
    with gateway_test_client() as client:
        response = client.get(f"/api/agents/{aid}/inspect/live")
    assert response.status_code == 200
    assert response.json()["shells"] == []
    assert response.json()["shells_available"] is False


@pytest.mark.parametrize("malformed", [False, True])
def test_inspect_live_distinguishes_valid_empty_from_missing_shell_data(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, malformed: bool
) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()

    async def probe(*args: object, **kwargs: object) -> dict[str, object]:
        return {} if malformed else {"shells": []}

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", probe)
    with gateway_test_client() as client:
        if malformed:
            with pytest.raises(KeyError, match="shells"):
                client.get(f"/api/agents/{aid}/inspect/live")
        else:
            response = client.get(f"/api/agents/{aid}/inspect/live")
            assert response.status_code == 200
            assert response.json()["shells"] == []
            assert response.json()["shells_available"] is True


def test_inspect_config_overlay_roundtrips(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(
        db_conn,
        config_overlay={"llm_model": "claude-opus-4-8", "auto_compact_fraction": 0.7},
    )
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert body["agent_id"] == aid
    assert body["config_overlay"] == {
        "llm_model": "claude-opus-4-8",
        "auto_compact_fraction": 0.7,
    }
    assert isinstance(body["shells"], list)


def test_inspect_null_config_overlay_is_empty_dict(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, config_overlay=None)
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert body["config_overlay"] == {}


def test_inspect_shells_probed_on_agents_machine(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'runner-a' WHERE id = %s", (aid,))
    db_conn.commit()

    seen: dict[str, object] = {}

    async def _fake_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        seen["machine"] = target_machine
        seen["kind"] = kind
        seen["payload"] = payload
        return {
            "shells": [
                {"id": 5, "name": "desktop-remove", "created_at": None, "uptime_seconds": 42},
            ]
        }

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _fake_dispatch)

    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert seen == {
        "machine": "runner-a",
        "kind": "shell_probe",
        "payload": {"agent_id": aid},
    }
    assert body["shells"] == [
        {
            "id": 5,
            "name": "desktop-remove",
            "created_at": None,
            "uptime_seconds": 42,
            "expires_at": None,
        }
    ]


@pytest.mark.parametrize("name", ["page-preview", "schedule-check"])
def test_inspect_shells_carry_ttl_deadline_from_gateway_db(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    launched = datetime.now(UTC) - timedelta(minutes=30)
    deadline = datetime.now(UTC) + timedelta(hours=2)

    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'runner-a' WHERE id = %s", (aid,))
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) VALUES (%s, %s, %s)",
            (aid, 5, deadline),
        )
    db_conn.commit()

    async def _fake_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return {
            "shells": [
                {"id": 5, "name": "dev-server", "created_at": None, "uptime_seconds": 42},
                {"id": 6, "name": name, "created_at": launched.isoformat(), "uptime_seconds": 1800},
            ]
        }

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _fake_dispatch)

    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    by_id = {s["id"]: s for s in body["shells"]}
    assert datetime.fromisoformat(by_id[5]["expires_at"]) == deadline
    assert by_id[6]["expires_at"] is None


def test_inspect_shells_degrade_to_empty_on_unreachable(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'runner-a' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _unreachable_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise cluster_rpc.ClusterOpUnreachable("connect failed")

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _unreachable_dispatch)

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{aid}/inspect/live")
    assert resp.status_code == 200
    assert resp.json()["shells"] == []


def test_inspect_shells_degrade_to_empty_on_failed_op(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'runner-a' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _failed_dispatch(
        _db: object, target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise cluster_rpc.ClusterOpFailed({"error": "unknown kind: shell_probe"})

    monkeypatch.setattr(cluster_rpc, "dispatch_to_machine", _failed_dispatch)

    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{aid}/inspect/live")
    assert resp.status_code == 200
    assert resp.json()["shells"] == []


@pytest.mark.parametrize("bad", ["5", "-1", "169", "abc", "24.5"])
def test_inspect_invalid_hours_422(db_conn: psycopg.Connection, bad: str) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with gateway_test_client() as client:
        resp = client.get(f"/api/agents/{aid}/inspect/statistics", params={"hours": bad})
    assert resp.status_code == 422


def test_inspect_heartbeat_running_agent_dashes(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, status="running")
    db_conn.commit()
    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["interval_s"] == int(settings.daemon.heartbeat_interval_seconds)
    assert hb["next_at"] is None
    assert hb["paused_until"] is None
    assert hb["last_pause"] is None


def test_inspect_heartbeat_idle_projects_next_at(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=120)
    db_conn.commit()
    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["next_at"] is not None
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 120 + aid % JITTER_SPAN_S
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)


def test_jitter_span_s_stays_whole_seconds() -> None:
    assert int(JITTER_SPAN_S) == JITTER_SPAN_S


def test_inspect_heartbeat_paused_shows_window(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(
        db_conn, status="idling", status_changed_s_ago=600, paused_until_s_ahead=1800
    )
    db_conn.commit()
    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["next_at"] is None
    assert hb["paused_until"] is not None
    assert _seconds_from_now(hb["paused_until"]) == pytest.approx(1800, abs=5)


def test_inspect_heartbeat_expired_pause_is_not_paused(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(
        db_conn, status="idling", status_changed_s_ago=60, paused_until_s_ahead=-120
    )
    db_conn.commit()
    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["next_at"] is not None


def test_inspect_heartbeat_pending_inbound_marks_heartbeat_pending(
    db_conn: psycopg.Connection,
) -> None:
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=500)
    _insert_pending_inbound(db_conn, agent_id=aid)
    db_conn.commit()
    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["heartbeat_pending"] is True
    assert hb["next_at"] is None


def test_inspect_heartbeat_no_pending_projects_from_last_active(
    db_conn: psycopg.Connection,
) -> None:
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=120)
    db_conn.commit()
    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["heartbeat_pending"] is False
    assert hb["paused_until"] is None
    assert hb["next_at"] is not None
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 120 + aid % JITTER_SPAN_S
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)


def test_inspect_heartbeat_consumed_checkin_uses_durable_reminder_floor(
    db_conn: psycopg.Connection,
) -> None:
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=600)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET last_heartbeat_at = now() - interval '120 seconds' "
            "WHERE id = %s",
            (aid,),
        )
    db_conn.commit()

    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]

    assert hb["heartbeat_pending"] is False
    assert hb["paused_until"] is None
    assert hb["next_at"] is not None
    expected = settings.daemon.heartbeat_interval_seconds - 120
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)


def test_inspect_heartbeat_stuck_after_expired_pause_no_past_next_at(
    db_conn: psycopg.Connection,
) -> None:
    aid = _insert_agent(
        db_conn,
        status="idling",
        status_changed_s_ago=7200,
        paused_until_s_ahead=-6900,
    )
    _insert_pending_inbound(db_conn, agent_id=aid)
    db_conn.commit()
    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["next_at"] is None
    assert hb["heartbeat_pending"] is True


def test_inspect_heartbeat_overdue_still_projects_raw_next_at(
    db_conn: psycopg.Connection,
) -> None:
    aid = _insert_agent(
        db_conn,
        status="idling",
        status_changed_s_ago=7200,
    )
    db_conn.commit()
    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["heartbeat_pending"] is False
    assert hb["next_at"] is not None
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 7200 + aid % JITTER_SPAN_S
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)


def test_inspect_heartbeat_stale_pending_does_not_suppress(
    db_conn: psycopg.Connection,
) -> None:
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=120)
    _insert_pending_inbound(db_conn, agent_id=aid, created_s_ago=STALE_PENDING_S + 300)
    db_conn.commit()
    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["heartbeat_pending"] is False
    assert hb["next_at"] is not None
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 120 + aid % JITTER_SPAN_S
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)


def test_inspect_heartbeat_last_pause_newest_wins(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=60)
    other = _insert_agent(db_conn, status="idling", status_changed_s_ago=60)
    _pause_row(db_conn, agent_id=aid, duration_s=3600, hours_ago=5)
    _pause_row(db_conn, agent_id=aid, duration_s=1800, hours_ago=1)
    _pause_row(db_conn, agent_id=other, duration_s=999, hours_ago=0)
    db_conn.commit()
    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["last_pause"] is not None
    assert hb["last_pause"]["duration_s"] == 1800
    assert _seconds_from_now(hb["last_pause"]["at"]) == pytest.approx(-3600, abs=60)


def test_inspect_heartbeat_last_pause_beyond_lookback_is_none(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=60)
    _pause_row(db_conn, agent_id=aid, duration_s=3600, hours_ago=30)
    db_conn.commit()
    with gateway_test_client() as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["last_pause"] is None


def test_inspect_notice_when_agent_has_open_require_response(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, content, priority, require_response, blocking, expire_at) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, "
            "'Approve deploy?', 'Can we deploy to prod?', 'P0', true, true, now() + interval '1 day') "
            "RETURNING id, created_at",
            (aid, aid),
        )
        row = cur.fetchone()
    assert row is not None
    nid, _created_at = row
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    notice = body["notice"]
    assert notice is not None
    assert notice["id"] == nid
    assert notice["title"] == "Approve deploy?"
    assert notice["content"] == "Can we deploy to prod?"
    assert notice["priority"] == "P0"
    assert notice["require_response"] is True
    assert notice["blocking"] is True


def test_inspect_notice_when_agent_has_open_fyi(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, content, priority, require_response, blocking, expire_at) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, "
            "'Milestone reached', NULL, 'P2', false, false, now() + interval '1 day') "
            "RETURNING id",
            (aid, aid),
        )
        row = cur.fetchone()
    assert row is not None
    nid = row[0]
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    notice = body["notice"]
    assert notice is not None
    assert notice["id"] == nid
    assert notice["require_response"] is False
    assert notice["blocking"] is False
    assert notice["content"] is None


def test_inspect_notice_when_agent_has_none(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert body["notice"] is None


def test_inspect_notice_resolved_not_returned(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking, "
            "resolved_at, resolution, reply, expire_at) VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices "
            "WHERE agent_id = %s), -1) + 1, 'Old', 'P3', true, false, now(), 'answered', 'done', now() + interval '1 day')",
            (aid, aid),
        )
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking, expire_at) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, "
            "'Current', 'P1', false, false, now() + interval '1 day')",
            (aid, aid),
        )
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert body["notice"] is not None
    assert body["notice"]["title"] == "Current"


def test_inspect_notice_other_agent_not_visible(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    other = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking, expire_at) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, "
            "'Other notice', 'P0', true, true, now() + interval '1 day')",
            (other, other),
        )
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert body["notice"] is None


def test_inspect_live_reads_committed_pause(
    db_conn: psycopg.Connection,
) -> None:
    aid = _insert_agent(db_conn, status="idling")
    _pause_row(db_conn, agent_id=aid, duration_s=900, hours_ago=1)
    db_conn.commit()

    with gateway_test_client() as client:
        response = client.get(f"/api/agents/{aid}/inspect/live")
    assert response.status_code == 200
    assert response.json()["heartbeat"]["last_pause"]["duration_s"] == 900


def _pause_row(
    conn: psycopg.Connection, *, agent_id: int, duration_s: float, hours_ago: float
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO heartbeat_pause_log (agent_id, duration_s, created_at) "
            "VALUES (%s, %s, now() - make_interval(secs => %s))",
            (agent_id, duration_s, hours_ago * 3600),
        )


def test_inspect_last_pause_same_timestamp_uses_newest_id(db_conn: psycopg.Connection) -> None:
    aid = _insert_agent(db_conn)
    _pause_row(db_conn, agent_id=aid, duration_s=3600, hours_ago=1)
    _pause_row(db_conn, agent_id=aid, duration_s=600, hours_ago=1)
    db_conn.commit()
    with gateway_test_client() as client:
        response = client.get(f"/api/agents/{aid}/inspect/live")
    assert response.json()["heartbeat"]["last_pause"]["duration_s"] == 600
