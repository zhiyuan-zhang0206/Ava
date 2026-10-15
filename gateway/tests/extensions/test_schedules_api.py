from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from tests.fixtures.gateway_config import gateway_test_client


def _create(client: TestClient, **kw: object):
    body = {"name": "s", "script": "print(1)\n", **kw}
    return client.post("/api/schedules", json=body)


def _versions(conn: psycopg.Connection, name: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.note FROM schedule_versions v JOIN schedules s ON s.id = v.schedule_id "
            "WHERE s.name = %s ORDER BY v.id",
            (name,),
        )
        return [r[0] for r in cur.fetchall()]


class TestCreate:
    def test_create_returns_full_view_and_initial_version(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            r = _create(client, name="daily", script="print(1)\n", description="hi")
        assert r.status_code == 201
        body = r.json()
        assert body["name"] == "daily"
        assert body["enabled"] is True
        assert body["status"] == "stopped"
        assert body["command"] == "python schedule.py"
        assert body["script"] == "print(1)\n"
        assert body["description"] == "hi"
        assert _versions(db_conn, "daily") == ["initial"]

    def test_create_bad_script_400(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            r = _create(client, name="bad", script="def (:\n")
        assert r.status_code == 400
        assert "syntax error" in r.json()["detail"]

    def test_create_duplicate_name_409(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            assert _create(client, name="dup").status_code == 201
            r = _create(client, name="dup")
        assert r.status_code == 409


class TestReadUpdateDelete:
    def test_get_missing_404(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            assert client.get("/api/schedules/9999").status_code == 404

    def test_list_omits_script(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            _create(client, name="a")
            rows = client.get("/api/schedules").json()
        assert [r["name"] for r in rows] == ["a"]
        assert "script" not in rows[0]
        assert rows[0]["enabled"] is True

    def test_put_description_only_writes_no_new_version(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="e").json()["id"]
            r = client.put(f"/api/schedules/{sid}", json={"description": "changed"})
        assert r.status_code == 200
        assert r.json()["description"] == "changed"
        assert _versions(db_conn, "e") == ["initial"]

    def test_put_invalid_script_400(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="e").json()["id"]
            r = client.put(f"/api/schedules/{sid}", json={"script": "def (:\n"})
        assert r.status_code == 400

    def test_put_script_writes_edit_version(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="e").json()["id"]
            r = client.put(f"/api/schedules/{sid}", json={"script": "print(2)\n"})
        assert r.status_code == 200
        assert r.json()["script"] == "print(2)\n"
        assert _versions(db_conn, "e") == ["initial", "edit"]

    def test_put_enabled_change_routes_through_immediate_session_sync(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gateway.schedules import session_control

        synced: list[int] = []

        async def _record_sync(pool: object, schedule_id: int) -> None:
            synced.append(schedule_id)

        monkeypatch.setattr(session_control, "wait_consumed", _record_sync)
        with gateway_test_client() as client:
            sid = _create(client, name="e").json()["id"]
            stopped = client.put(f"/api/schedules/{sid}", json={"enabled": False})

        assert stopped.status_code == 200
        assert stopped.json()["enabled"] is False
        assert synced == [sid]

    def test_put_same_enabled_value_does_not_sync(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gateway.schedules import session_control

        synced: list[int] = []

        async def _record_sync(pool: object, schedule_id: int) -> None:
            synced.append(schedule_id)

        monkeypatch.setattr(session_control, "wait_consumed", _record_sync)
        with gateway_test_client() as client:
            sid = _create(client, name="e").json()["id"]
            enabled = client.put(f"/api/schedules/{sid}", json={"enabled": True})

        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert synced == []

    def test_put_no_fields_400(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="e").json()["id"]
            assert client.put(f"/api/schedules/{sid}", json={}).status_code == 400

    def test_delete_then_404(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="d").json()["id"]
            assert client.delete(f"/api/schedules/{sid}").status_code == 200
            assert client.get(f"/api/schedules/{sid}").status_code == 404
            assert client.delete(f"/api/schedules/{sid}").status_code == 404


class TestSyncRequests:
    def test_control_writes_one_queued_request_per_schedule(
        self, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="q", enabled=False).json()["id"]
            client.post(f"/api/schedules/{sid}/start")
            client.post(f"/api/schedules/{sid}/stop")
            assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [
                (sid,)
            ]
            db_conn.execute("DELETE FROM schedule_sync_requests")
            db_conn.commit()
            assert client.delete(f"/api/schedules/{sid}").status_code == 200
            assert db_conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [
                (sid,)
            ]


class TestControl:
    def test_start_stop_toggle_enabled_and_sync_the_session(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gateway.schedules import session_control

        synced: list[int] = []

        async def _record_sync(pool: object, schedule_id: int) -> None:
            synced.append(schedule_id)

        monkeypatch.setattr(session_control, "wait_consumed", _record_sync)
        with gateway_test_client() as client:
            sid = _create(client, name="c", enabled=False).json()["id"]
            assert client.post(f"/api/schedules/{sid}/start").json()["enabled"] is True
            stopped = client.post(f"/api/schedules/{sid}/stop").json()
        assert stopped["enabled"] is False
        assert synced == [sid, sid]

    def test_restart_disabled_409(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="c", enabled=False).json()["id"]
            r = client.post(f"/api/schedules/{sid}/restart")
        assert r.status_code == 409

    def test_restart_enabled_ok(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="c", enabled=True).json()["id"]
            assert client.post(f"/api/schedules/{sid}/restart").status_code == 200


class TestLogsRuns:
    def test_logs_falls_back_to_last_error(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="l").json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "UPDATE schedules SET last_error = %s WHERE id = %s", ("boom\ntrace", sid)
                )
            db_conn.commit()
            body = client.get(f"/api/schedules/{sid}/logs").json()
        assert body["source"] == "last_error"
        assert body["lines"] == ["boom", "trace"]

    def test_logs_strips_trailing_blank_rows(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gateway.schedules import session_control

        async def _fake_capture(schedule_id: int, lines: int) -> str:
            return "line one\nline two\n\n\n\n"

        monkeypatch.setattr(session_control, "capture", _fake_capture)
        with gateway_test_client() as client:
            sid = _create(client, name="l").json()["id"]
            body = client.get(f"/api/schedules/{sid}/logs").json()
        assert body["source"] == "live"
        assert body["lines"] == ["line one", "line two"]

    def test_logs_small_lines_window_expands_to_display_height(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gateway.schedules import session_control

        seen: list[int] = []

        async def _fake_capture(schedule_id: int, lines: int) -> str:
            seen.append(lines)
            return "line one\nline two\n\n\n\n"

        monkeypatch.setattr(session_control, "capture", _fake_capture)
        with gateway_test_client() as client:
            sid = _create(client, name="l").json()["id"]
            body = client.get(f"/api/schedules/{sid}/logs?lines=3").json()
        assert seen == [200]
        assert body["source"] == "live"
        assert body["lines"] == ["line one", "line two"]

    def test_logs_none_when_no_output(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="l").json()["id"]
            body = client.get(f"/api/schedules/{sid}/logs").json()
        assert body["source"] == "none"
        assert body["lines"] == []

    def test_logs_reads_pty_transcript_for_dead_session(self, db_conn: psycopg.Connection) -> None:
        from base.cluster import session_name
        from base.sessions.backend import get_shell_backend

        with gateway_test_client() as client:
            sid = _create(client, name="l").json()["id"]
            log_path = get_shell_backend().session_log_path(session_name(f"schedule-{sid}"))
            assert log_path is not None
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("line one\nline two\n")
            body = client.get(f"/api/schedules/{sid}/logs").json()
        assert body["source"] == "transcript"
        assert body["lines"] == ["line one", "line two"]

    def test_runs_empty(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="r").json()["id"]
            assert client.get(f"/api/schedules/{sid}/runs").json() == []

    def test_runs_default_limit_comes_from_display_config(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from base.config import settings

        monkeypatch.setattr(settings.display, "schedules_runs_default_limit", 1)
        with gateway_test_client() as client:
            sid = _create(client, name="r").json()["id"]
            with db_conn.cursor() as cur:
                cur.execute("INSERT INTO schedule_runs (schedule_id) VALUES (%s)", (sid,))
                cur.execute("INSERT INTO schedule_runs (schedule_id) VALUES (%s)", (sid,))
            db_conn.commit()
            body = client.get(f"/api/schedules/{sid}/runs").json()
        assert len(body) == 1

    def test_runs_missing_schedule_404(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            assert client.get("/api/schedules/9999/runs").status_code == 404

    def test_runs_shows_in_progress_null_row(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="r2").json()["id"]
            with db_conn.cursor() as cur:
                cur.execute("INSERT INTO schedule_runs (schedule_id) VALUES (%s)", (sid,))
            db_conn.commit()
            body = client.get(f"/api/schedules/{sid}/runs").json()
        assert len(body) == 1
        assert body[0]["ok"] is None
        assert body[0]["note"] is None

    def test_runs_orders_newest_first_with_id_tiebreak(self, db_conn: psycopg.Connection) -> None:
        with gateway_test_client() as client:
            sid = _create(client, name="r3").json()["id"]
            with db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO schedule_runs (schedule_id, ran_at) "
                    "VALUES (%s, now() - interval '1 hour') RETURNING id",
                    (sid,),
                )
                row = cur.fetchone()
                assert row is not None
                rid_old = row[0]
                cur.execute(
                    "INSERT INTO schedule_runs (schedule_id, ran_at) "
                    "VALUES (%s, now() - interval '1 hour') RETURNING id",
                    (sid,),
                )
                row = cur.fetchone()
                assert row is not None
                rid_new = row[0]
            db_conn.commit()
            body = client.get(f"/api/schedules/{sid}/runs").json()
        assert [r["id"] for r in body] == [rid_new, rid_old]
