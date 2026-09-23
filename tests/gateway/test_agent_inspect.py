"""Current-state Inspector HTTP contracts; persisted statistics have their own tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway import loki_events
from gateway.app import app
from gateway.routers import agent_inspect
from services.heartbeat import JITTER_SPAN_S, STALE_PENDING_S
from shared.config import settings


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
    """INSERT an agents_meta row. `status_changed_s_ago` backdates BOTH
    status_changed_at and last_active_at (the BEFORE-UPDATE-OF-status trigger does
    not fire on a timestamp-only update) — it models "the agent last did anything
    N seconds ago", and the heartbeat projection reads the real-activity clock
    (last_active_at). `paused_until_s_ahead` sets heartbeat_paused_until relative
    to now() — negative = an already-expired pause."""
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
    """Signed seconds between an ISO-8601 instant and now (future = positive)."""
    return (datetime.fromisoformat(iso) - datetime.now(UTC)).total_seconds()


def _insert_pending_inbound(
    db: psycopg.Connection,
    *,
    agent_id: int,
    kind: str = "heartbeat",
    created_s_ago: float | None = None,
) -> None:
    """INSERT a pending inbound_messages row — models a check-in (or any wake) the
    daemon has queued but the agent has not yet claimed. `status` defaults to
    'pending', so this is what the daemon's `NOT EXISTS (pending inbound)` guard
    (and now the inspector's `heartbeat_pending`) keys off. `created_s_ago`
    backdates created_at — a row older than `STALE_PENDING_S` is stale: the
    daemon re-checks-in past it (and the panel projects next_at instead)."""
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
    """No agents_meta row → 404 (fail-fast, no empty shell)."""
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get("/api/agents/999999/inspect/statistics")
    assert resp.status_code == 404


def test_inspect_live_returns_only_window_independent_fields(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live route is the cheap inspector skeleton, not the aggregate payload."""
    aid = _insert_agent(
        db_conn,
        status="idling",
        config_overlay={"llm_model": "claude-opus-4-8"},
    )
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'wsl' WHERE id = %s", (aid,))
    _pause_row(db_conn, agent_id=aid, duration_s=1800, hours_ago=1)
    db_conn.commit()

    async def dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        assert (target_machine, kind, payload) == (
            "wsl",
            "shell_probe",
            {"agent_id": aid},
        )
        return {
            "shells": [{"id": 5, "name": "live-shell", "created_at": None, "uptime_seconds": 42}]
        }

    monkeypatch.setattr(agent_inspect._cluster_rpc, "dispatch_to_machine", dispatch)
    with TestClient(app) as client:
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
        "preset_name",
        "notice",
        "heartbeat",
    }
    assert body["agent_id"] == aid
    assert body["machine"] == "wsl"
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
    with TestClient(app) as client:
        response = client.get("/api/agents/999999/inspect/live")
    assert response.status_code == 404


def test_inspect_live_probe_failure_is_unavailable_not_empty_success(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()

    async def unreachable(*args: object, **kwargs: object) -> dict[str, object]:
        raise agent_inspect._cluster_rpc.ClusterOpUnreachable("connect failed")

    monkeypatch.setattr(agent_inspect._cluster_rpc, "dispatch_to_machine", unreachable)
    with TestClient(app) as client:
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

    monkeypatch.setattr(agent_inspect._cluster_rpc, "dispatch_to_machine", probe)
    with TestClient(app) as client:
        if malformed:
            with pytest.raises(KeyError, match="shells"):
                client.get(f"/api/agents/{aid}/inspect/live")
        else:
            response = client.get(f"/api/agents/{aid}/inspect/live")
            assert response.status_code == 200
            assert response.json()["shells"] == []
            assert response.json()["shells_available"] is True


def test_inspect_config_overlay_roundtrips(db_conn: psycopg.Connection) -> None:
    """config_overlay JSONB pass-through as-is; shells is a list, machine echoed."""
    aid = _insert_agent(
        db_conn,
        config_overlay={"llm_model": "claude-opus-4-8", "auto_compact_fraction": 0.7},
    )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert body["agent_id"] == aid
    assert body["config_overlay"] == {
        "llm_model": "claude-opus-4-8",
        "auto_compact_fraction": 0.7,
    }
    assert isinstance(body["shells"], list)


def test_inspect_null_config_overlay_is_empty_dict(db_conn: psycopg.Connection) -> None:
    """config_overlay NULL (cluster defaults) → {} not null."""
    aid = _insert_agent(db_conn, config_overlay=None)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert body["config_overlay"] == {}


# ── Shells section (shell_probe op, uniform machine path) ─────────────────────


def test_inspect_shells_probed_on_agents_machine(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """shells come from a `shell_probe` op dispatched to the agent's machine —
    a remote runner's live shells appear exactly like a local one's (no local
    session probing in the gateway)."""
    from gateway.routers import agent_inspect as inspect_mod

    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'wsl' WHERE id = %s", (aid,))
    db_conn.commit()

    seen: dict[str, object] = {}

    async def _fake_dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        seen["machine"] = target_machine
        seen["kind"] = kind
        seen["payload"] = payload
        return {
            "shells": [
                {"id": 5, "name": "desktop-remove", "created_at": None, "uptime_seconds": 42},
            ]
        }

    monkeypatch.setattr(inspect_mod._cluster_rpc, "dispatch_to_machine", _fake_dispatch)

    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert seen == {
        "machine": "wsl",
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
    """Only recorded TTLs enrich the probe. Row-less page/schedule sessions
    render no TTL even with a launch epoch (task #4086, DP1)."""
    launched = datetime.now(UTC) - timedelta(minutes=30)
    deadline = datetime.now(UTC) + timedelta(hours=2)

    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'wsl' WHERE id = %s", (aid,))
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) VALUES (%s, %s, %s)",
            (aid, 5, deadline),
        )
    db_conn.commit()

    async def _fake_dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        return {
            "shells": [
                {"id": 5, "name": "dev-server", "created_at": None, "uptime_seconds": 42},
                {"id": 6, "name": name, "created_at": launched.isoformat(), "uptime_seconds": 1800},
            ]
        }

    monkeypatch.setattr(agent_inspect._cluster_rpc, "dispatch_to_machine", _fake_dispatch)

    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    by_id = {s["id"]: s for s in body["shells"]}
    assert datetime.fromisoformat(by_id[5]["expires_at"]) == deadline
    assert by_id[6]["expires_at"] is None  # no row -> no TTL


def test_inspect_shells_degrade_to_empty_on_unreachable(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable machine (or unregistered name) degrades to an empty shell
    list — the inspector shows 'None open' instead of 503ing the whole panel."""
    from gateway.routers import agent_inspect as inspect_mod
    from ops import cluster_rpc

    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'wsl' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _unreachable_dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise cluster_rpc.ClusterOpUnreachable("connect failed")

    monkeypatch.setattr(inspect_mod._cluster_rpc, "dispatch_to_machine", _unreachable_dispatch)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/live")
    assert resp.status_code == 200
    assert resp.json()["shells"] == []


def test_inspect_shells_degrade_to_empty_on_failed_op(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A version-skewed runner that does not know the op reports 'failed' —
    same graceful degradation to an empty shell list."""
    from gateway.routers import agent_inspect as inspect_mod
    from ops import cluster_rpc

    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE agents_meta SET machine = 'wsl' WHERE id = %s", (aid,))
    db_conn.commit()

    async def _failed_dispatch(
        target_machine: str, kind: str, payload: dict[str, object], **kwargs: object
    ) -> dict[str, object]:
        raise cluster_rpc.ClusterOpFailed({"error": "unknown kind: shell_probe"})

    monkeypatch.setattr(inspect_mod._cluster_rpc, "dispatch_to_machine", _failed_dispatch)

    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/live")
    assert resp.status_code == 200
    assert resp.json()["shells"] == []


@pytest.mark.parametrize("bad", ["5", "-1", "169", "abc", "24.5"])
def test_inspect_invalid_hours_422(db_conn: psycopg.Connection, bad: str) -> None:
    """hours outside {0,1,6,24,72,168} → 422 (fail-fast, reusing StatsWindowHours)."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with TestClient(app) as client:
        resp = client.get(f"/api/agents/{aid}/inspect/statistics", params={"hours": bad})
    assert resp.status_code == 422


# ── Heartbeat section ──────────────────────────────────────────────────────


def test_inspect_heartbeat_running_agent_dashes(db_conn: psycopg.Connection) -> None:
    """running agent does not receive check-ins → next_at/paused_until both None; never paused →
    last_pause None. interval_s echoes the configuration value."""
    aid = _insert_agent(db_conn, status="running")
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["interval_s"] == int(settings.daemon.heartbeat_interval_seconds)
    assert hb["next_at"] is None
    assert hb["paused_until"] is None
    assert hb["last_pause"] is None


def test_inspect_heartbeat_idle_projects_next_at(db_conn: psycopg.Connection) -> None:
    """idle + no pause → next_at = effective_last_active + idle_threshold_s +
    per-agent jitter (id mod JITTER_SPAN_S), exactly the daemon's due-time;
    paused_until None. Parked 120s ago, so next_at ≈ now + (idle_threshold - 120)s."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=120)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["next_at"] is not None
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 120 + aid % JITTER_SPAN_S
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_heartbeat_zero_jitter_span_disables_jitter(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JITTER_SPAN_S=0 must disable jitter exactly like the daemon's
    `NULLIF(span, 0)` collapse (QA #952 b): the projection guards the modulo,
    so the endpoint still serves next_at = last_active + idle_threshold with no
    jitter term instead of ZeroDivisionError-ing the inspect response."""
    monkeypatch.setattr("gateway.routers._inspect_live.JITTER_SPAN_S", 0)
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=120)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["heartbeat_pending"] is False
    assert hb["next_at"] is not None
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 120
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)  # pyright: ignore[reportUnknownMemberType]


def test_jitter_span_s_stays_whole_seconds() -> None:
    """The jitter span must remain a whole-second count: the daemon SQL casts it
    with Postgres' `::int` (rounds half away from zero) while the inspector
    computes the offset in Python (`int` truncates) — a non-integral span would
    split the two interpretations again into a 1s drift (QA #952 b)."""
    assert int(JITTER_SPAN_S) == JITTER_SPAN_S


def test_inspect_heartbeat_paused_shows_window(db_conn: psycopg.Connection) -> None:
    """idle + future heartbeat_paused_until → paused_until pass-through, next_at None."""
    aid = _insert_agent(
        db_conn, status="idling", status_changed_s_ago=600, paused_until_s_ahead=1800
    )
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["next_at"] is None
    assert hb["paused_until"] is not None
    assert _seconds_from_now(hb["paused_until"]) == pytest.approx(1800, abs=5)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_heartbeat_expired_pause_is_not_paused(db_conn: psycopg.Connection) -> None:
    """Expired pause (heartbeat_paused_until in the past) treated as no pause → next_at projected normally,
    paused_until None. Consistent with daemon's `<= now()` check."""
    aid = _insert_agent(
        db_conn, status="idling", status_changed_s_ago=60, paused_until_s_ahead=-120
    )
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["next_at"] is not None


def test_inspect_heartbeat_pending_inbound_marks_heartbeat_pending(
    db_conn: psycopg.Connection,
) -> None:
    """idle + no pause + has pending inbound (daemon just sent check-in but agent hasn't processed) →
    heartbeat_pending=True, next_at None. Mirrors daemon's `NOT EXISTS (pending inbound)`
    guard: since a wake is already queued, daemon will not enqueue another check-in, so no future
    time can be projected. last_active_at stopped 500s ago (long expired), if projected normally
    would yield a 'past' next_at — exactly the root cause of the stuck agent showing 'one hour ago'
    in the original bug."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=500)
    _insert_pending_inbound(db_conn, agent_id=aid)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["heartbeat_pending"] is True
    assert hb["next_at"] is None


def test_inspect_heartbeat_no_pending_projects_from_last_active(
    db_conn: psycopg.Connection,
) -> None:
    """No pending inbound → next_at is based on last_active_at, unaffected by historic
    heartbeat_nudged events: once a check-in is processed and consumed, inbound is no longer pending,
    and that turn pushed last_active_at past the check-in time, so last_active_at alone is the correct
    baseline (the old event-floor was therefore redundant). heartbeat_pending False."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=120)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["heartbeat_pending"] is False
    assert hb["paused_until"] is None
    assert hb["next_at"] is not None
    # next_at is based on last_active_at (120s ago) + per-agent jitter.
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 120 + aid % JITTER_SPAN_S
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_heartbeat_consumed_checkin_uses_durable_reminder_floor(
    db_conn: psycopg.Connection,
) -> None:
    """A consumed no-turn heartbeat must defer the inspector's next check-in too."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=600)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET last_heartbeat_at = now() - interval '120 seconds' "
            "WHERE id = %s",
            (aid,),
        )
    db_conn.commit()

    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]

    assert hb["heartbeat_pending"] is False
    assert hb["paused_until"] is None
    assert hb["next_at"] is not None
    expected = settings.daemon.heartbeat_interval_seconds - 120
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_heartbeat_stuck_after_expired_pause_no_past_next_at(
    db_conn: psycopg.Connection,
) -> None:
    """Regression (original bug): agent paused 5 minutes two hours ago, then stuck and not processing
    check-ins. The panel should show heartbeat_pending (a check-in is already queued), not project
    next_at as 'one hour ago' (a past time). Reproduction condition: last_active_at stopped 2h ago,
    pause expired, one pending heartbeat inbound (daemon because of pending guard does not re-send)."""
    aid = _insert_agent(
        db_conn,
        status="idling",
        status_changed_s_ago=7200,  # last_active_at 2h ago
        paused_until_s_ahead=-6900,  # paused_until 1h55m ago (expired)
    )
    # daemon sent a check-in after pause expired, but agent stuck and never processed → inbound still pending.
    _insert_pending_inbound(db_conn, agent_id=aid)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    # expired pause treated as no pause.
    assert hb["paused_until"] is None
    # key assertion: does not project a past next_at (original bug would show "one hour ago").
    assert hb["next_at"] is None
    assert hb["heartbeat_pending"] is True


@pytest.mark.parametrize("status", ["restarting"])
def test_inspect_heartbeat_idle_family_projects_next_at(
    db_conn: psycopg.Connection, status: str
) -> None:
    """The fleet view projects restarting rows to "Idle", so their page must
    show a computable next check-in like a plain idle agent (user
    report 2026-08-28: a restarting agent rendered an empty cell). Parked 120s
    ago → next_at ≈ now + (idle_threshold - 120)s + jitter, exactly like
    idling."""
    aid = _insert_agent(db_conn, status=status, status_changed_s_ago=120)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["paused_until"] is None
    assert hb["heartbeat_pending"] is False
    assert hb["next_at"] is not None
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 120 + aid % JITTER_SPAN_S
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)  # pyright: ignore[reportUnknownMemberType]


@pytest.mark.parametrize("status", ["restarting"])
def test_inspect_heartbeat_idle_family_pending_inbound_marks_heartbeat_pending(
    db_conn: psycopg.Connection, status: str
) -> None:
    """Idle-family agents obey the same `NOT EXISTS (pending inbound)` guard as
    idling: a queued wake (e.g. the restart_completed marker a restarting agent
    is about to claim) means the daemon schedules nothing, so `heartbeat_pending`
    shows instead of a projected time."""
    aid = _insert_agent(db_conn, status=status, status_changed_s_ago=120)
    _insert_pending_inbound(db_conn, agent_id=aid)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["heartbeat_pending"] is True
    assert hb["next_at"] is None


def test_inspect_heartbeat_restarting_overdue_still_projects_raw_next_at(
    db_conn: psycopg.Connection,
) -> None:
    """A restarting agent's idle clock can run past its due time (the daemon
    does not check in while the process is down): the projection stays raw
    `last_active_at + idle_threshold + jitter` (a past instant), and the
    frontend renders a past next_at as "due" — never "4m ago" for a *next*
    heartbeat."""
    aid = _insert_agent(
        db_conn,
        status="restarting",
        status_changed_s_ago=7200,  # last_active_at 2h ago — far past due
    )
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["heartbeat_pending"] is False
    assert hb["next_at"] is not None
    # Raw projection: 2h ago + idle_threshold + jitter — clearly in the past.
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 7200 + aid % JITTER_SPAN_S
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_heartbeat_stale_pending_does_not_suppress(
    db_conn: psycopg.Connection,
) -> None:
    """A pending inbound older than the daemon's freshness window
    (STALE_PENDING_S=900s) no longer counts as "about to wake": the daemon
    re-checks-in on the agent, so the panel projects next_at instead of showing
    heartbeat_pending forever. Mirrors the daemon's windowed `NOT EXISTS` guard
    exactly (QA #877 N2) — the display never claims a stale wake is still
    suppressing check-ins."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=120)
    _insert_pending_inbound(db_conn, agent_id=aid, created_s_ago=STALE_PENDING_S + 300)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["heartbeat_pending"] is False
    assert hb["next_at"] is not None
    expected = settings.daemon.heartbeat_idle_threshold_seconds - 120 + aid % JITTER_SPAN_S
    assert _seconds_from_now(hb["next_at"]) == pytest.approx(expected, abs=5)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_heartbeat_last_pause_newest_wins(db_conn: psycopg.Connection) -> None:
    """last_pause takes the newest committed pause from this agent's trail;
    another agent's pause does not leak."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=60)
    other = _insert_agent(db_conn, status="idling", status_changed_s_ago=60)
    # 5h old pause + 1h new pause — new one wins
    _pause_row(db_conn, agent_id=aid, duration_s=3600, hours_ago=5)
    _pause_row(db_conn, agent_id=aid, duration_s=1800, hours_ago=1)
    # another agent's pause — must not appear in this agent's last_pause
    _pause_row(db_conn, agent_id=other, duration_s=999, hours_ago=0)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["last_pause"] is not None
    assert hb["last_pause"]["duration_s"] == 1800
    # at ≈ now - 1h
    assert _seconds_from_now(hb["last_pause"]["at"]) == pytest.approx(-3600, abs=60)  # pyright: ignore[reportUnknownMemberType]


def test_inspect_heartbeat_last_pause_beyond_lookback_is_none(db_conn: psycopg.Connection) -> None:
    """A pause older than the recent-history lookback is omitted."""
    aid = _insert_agent(db_conn, status="idling", status_changed_s_ago=60)
    _pause_row(db_conn, agent_id=aid, duration_s=3600, hours_ago=30)
    db_conn.commit()
    with TestClient(app) as client:
        hb = client.get(f"/api/agents/{aid}/inspect/live").json()["heartbeat"]
    assert hb["last_pause"] is None


# ── Notice section ──────────────────────────────────────────────────────────


def test_inspect_notice_when_agent_has_open_require_response(db_conn: psycopg.Connection) -> None:
    """An agent with a require_response notice → notice field present with all keys."""
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
    with TestClient(app) as client:
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
    """An agent with an FYI notice → notice field present with require_response=False."""
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
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    notice = body["notice"]
    assert notice is not None
    assert notice["id"] == nid
    assert notice["require_response"] is False
    assert notice["blocking"] is False
    assert notice["content"] is None


def test_inspect_notice_when_agent_has_none(db_conn: psycopg.Connection) -> None:
    """An agent with no open notices → notice is None (not missing, not empty dict)."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert body["notice"] is None


def test_inspect_notice_resolved_not_returned(db_conn: psycopg.Connection) -> None:
    """A resolved notice is not returned; only open (resolved_at IS NULL) counts."""
    aid = _insert_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking, "
            "resolved_at, resolution, reply, expire_at) VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices "
            "WHERE agent_id = %s), -1) + 1, 'Old', 'P3', true, false, now(), 'answered', 'done', now() + interval '1 day')",
            (aid, aid),
        )
        # And a newer open one — the newer wins since we ORDER BY created_at DESC LIMIT 1
        cur.execute(
            "INSERT INTO agent_notices (agent_id, local_id, title, priority, require_response, blocking, expire_at) "
            "VALUES (%s, COALESCE((SELECT MAX(local_id) FROM agent_notices WHERE agent_id = %s), -1) + 1, "
            "'Current', 'P1', false, false, now() + interval '1 day')",
            (aid, aid),
        )
    db_conn.commit()
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert body["notice"] is not None
    assert body["notice"]["title"] == "Current"


def test_inspect_notice_other_agent_not_visible(db_conn: psycopg.Connection) -> None:
    """Another agent's notice does not leak into this agent's inspect."""
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
    with TestClient(app) as client:
        body = client.get(f"/api/agents/{aid}/inspect/live").json()
    assert body["notice"] is None


# ── Activity (active-rate) section ────────────────────────────────────────────


# ── Response-cache discipline (the panel refetches in bursts) ─────────────────


def test_inspect_live_reads_committed_pause_when_all_log_reads_fail(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current state and its pause hint remain complete during a Loki outage."""
    aid = _insert_agent(db_conn, status="idling")
    _pause_row(db_conn, agent_id=aid, duration_s=900, hours_ago=1)
    db_conn.commit()

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("live inspector must not query telemetry")

    for name in ("query_events", "query_projected_lines", "attribute_aggregate"):
        monkeypatch.setattr(loki_events, name, unavailable)
    with TestClient(app) as client:
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
    """Two pauses in one transaction share now(); the later insert wins."""
    aid = _insert_agent(db_conn)
    _pause_row(db_conn, agent_id=aid, duration_s=3600, hours_ago=1)
    _pause_row(db_conn, agent_id=aid, duration_s=600, hours_ago=1)
    db_conn.commit()
    with TestClient(app) as client:
        response = client.get(f"/api/agents/{aid}/inspect/live")
    assert response.json()["heartbeat"]["last_pause"]["duration_s"] == 600
