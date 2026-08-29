"""Tests for gateway/routers/schedules.py — the /api/schedules HTTP surface.

Driven through TestClient(app) against the real test DB. The manager's session
control paths (sync/capture) are neutralized by the autouse
`_guard_schedule_manager` fixture, so these assert HTTP + DB behavior; the
manager's real convergence logic is covered in test_schedule_manager.py.
"""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient

from gateway.app import app
from shared.builtin_schedules import load_manifest

BUILTIN_MANIFEST_NAMES = [s.name for s in load_manifest()]


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
        with TestClient(app) as client:
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
        with TestClient(app) as client:
            r = _create(client, name="bad", script="def (:\n")
        assert r.status_code == 400
        assert "syntax error" in r.json()["detail"]

    def test_create_duplicate_name_409(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            assert _create(client, name="dup").status_code == 201
            r = _create(client, name="dup")
        assert r.status_code == 409


class TestReadUpdateDelete:
    def test_get_missing_404(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            assert client.get("/api/schedules/9999").status_code == 404

    def test_list_omits_script(self, db_conn: psycopg.Connection) -> None:
        # The lifespan provisions the repo's built-in schedules on boot, so the
        # list is the created row plus the manifest's built-ins.
        with TestClient(app) as client:
            _create(client, name="a")
            rows = client.get("/api/schedules").json()
        assert len(rows) == 1 + len(BUILTIN_MANIFEST_NAMES)
        assert "script" not in rows[0]
        by_name = {r["name"]: r for r in rows}
        assert by_name["a"]["enabled"] is True
        assert set(by_name) == set(BUILTIN_MANIFEST_NAMES) | {"a"}

    def test_put_description_only_writes_no_new_version(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            sid = _create(client, name="e").json()["id"]
            r = client.put(f"/api/schedules/{sid}", json={"description": "changed"})
        assert r.status_code == 200
        assert r.json()["description"] == "changed"
        assert _versions(db_conn, "e") == ["initial"]  # no new version for a metadata-only edit

    def test_put_invalid_script_400(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            sid = _create(client, name="e").json()["id"]
            r = client.put(f"/api/schedules/{sid}", json={"script": "def (:\n"})
        assert r.status_code == 400

    def test_put_script_writes_edit_version(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            sid = _create(client, name="e").json()["id"]
            r = client.put(f"/api/schedules/{sid}", json={"script": "print(2)\n"})
        assert r.status_code == 200
        assert r.json()["script"] == "print(2)\n"
        assert _versions(db_conn, "e") == ["initial", "edit"]

    def test_put_no_fields_400(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            sid = _create(client, name="e").json()["id"]
            assert client.put(f"/api/schedules/{sid}", json={}).status_code == 400

    def test_delete_then_404(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            sid = _create(client, name="d").json()["id"]
            assert client.delete(f"/api/schedules/{sid}").status_code == 200
            assert client.get(f"/api/schedules/{sid}").status_code == 404
            assert client.delete(f"/api/schedules/{sid}").status_code == 404


class TestControl:
    def test_start_stop_toggle_enabled(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            sid = _create(client, name="c", enabled=False).json()["id"]
            assert client.post(f"/api/schedules/{sid}/start").json()["enabled"] is True
            stopped = client.post(f"/api/schedules/{sid}/stop").json()
        assert stopped["enabled"] is False

    def test_restart_disabled_409(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            sid = _create(client, name="c", enabled=False).json()["id"]
            r = client.post(f"/api/schedules/{sid}/restart")
        assert r.status_code == 409

    def test_restart_enabled_ok(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            sid = _create(client, name="c", enabled=True).json()["id"]
            assert client.post(f"/api/schedules/{sid}/restart").status_code == 200


class TestLogsRunsDraft:
    def test_logs_falls_back_to_last_error(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
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
        """The live capture pads the screen to the terminal height — trailing
        blank rows are stripped so `logs --lines N` shows the content tail
        (the runner's output sits above blank display rows once scrolled)."""
        from gateway.schedule_manager import ScheduleManager

        async def _fake_capture(self: object, schedule_id: int, lines: int) -> str:
            return "line one\nline two\n\n\n\n"

        monkeypatch.setattr(ScheduleManager, "capture", _fake_capture)
        with TestClient(app) as client:
            sid = _create(client, name="l").json()["id"]
            body = client.get(f"/api/schedules/{sid}/logs").json()
        assert body["source"] == "live"
        assert body["lines"] == ["line one", "line two"]

    def test_logs_small_lines_window_expands_to_display_height(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `--lines N` smaller than the terminal height must still show the
        runner's output: the capture window widens to cover the display
        padding, blank rows are stripped, then the tail is trimmed to N."""
        from gateway.schedule_manager import ScheduleManager

        seen: list[int] = []

        async def _fake_capture(self: object, schedule_id: int, lines: int) -> str:
            seen.append(lines)
            return "line one\nline two\n\n\n\n"  # display-padded screen

        monkeypatch.setattr(ScheduleManager, "capture", _fake_capture)
        with TestClient(app) as client:
            sid = _create(client, name="l").json()["id"]
            body = client.get(f"/api/schedules/{sid}/logs?lines=3").json()
        assert seen == [200]  # window widened
        assert body["source"] == "live"
        assert body["lines"] == ["line one", "line two"]

    def test_logs_none_when_no_output(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            sid = _create(client, name="l").json()["id"]
            body = client.get(f"/api/schedules/{sid}/logs").json()
        assert body["source"] == "none"
        assert body["lines"] == []

    def test_logs_reads_pty_transcript_for_dead_session(self, db_conn: psycopg.Connection) -> None:
        """No live session -> the schedule session's PTY transcript file
        supplies the output: a finished/crashed runner's output survives the
        session being reaped, where scrollback was lost."""
        from shared.cluster import session_name
        from shared.session_backend import get_shell_backend

        with TestClient(app) as client:
            sid = _create(client, name="l").json()["id"]
            log_path = get_shell_backend().session_log_path(session_name(f"schedule-{sid}"))
            assert log_path is not None
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("line one\nline two\n")
            body = client.get(f"/api/schedules/{sid}/logs").json()
        assert body["source"] == "transcript"
        assert body["lines"] == ["line one", "line two"]

    def test_runs_empty(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            sid = _create(client, name="r").json()["id"]
            assert client.get(f"/api/schedules/{sid}/runs").json() == []

    def test_runs_missing_schedule_404(self, db_conn: psycopg.Connection) -> None:
        with TestClient(app) as client:
            assert client.get("/api/schedules/9999/runs").status_code == 404

    def test_runs_shows_in_progress_null_row(self, db_conn: psycopg.Connection) -> None:
        # QA P3-6: a run in progress (ok IS NULL) is visible in the run
        # history — the "…" row the drawer renders for a live run.
        with TestClient(app) as client:
            sid = _create(client, name="r2").json()["id"]
            with db_conn.cursor() as cur:
                cur.execute("INSERT INTO schedule_runs (schedule_id) VALUES (%s)", (sid,))
            db_conn.commit()
            body = client.get(f"/api/schedules/{sid}/runs").json()
        assert len(body) == 1
        assert body[0]["ok"] is None
        assert body[0]["note"] is None

    def test_runs_orders_newest_first_with_id_tiebreak(self, db_conn: psycopg.Connection) -> None:
        # QA P3-4: ORDER BY ran_at DESC needs a secondary key — two rows with
        # the same ran_at (same transaction timestamp here) must still come
        # back newest-id-first, deterministically.
        with TestClient(app) as client:
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

    def test_draft_spawns_writer_agent(
        self, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ops.rpc_schemas import SpawnAgentRequest, SpawnedAgent

        calls: dict[str, object] = {}

        async def _fake_create_launch(
            body: SpawnAgentRequest, target: str, pool: object
        ) -> SpawnedAgent:
            calls["label"] = body.label
            calls["prompt"] = body.prompt
            calls["target"] = target
            return SpawnedAgent(id=4242)

        monkeypatch.setattr(
            "gateway.routers.schedules.create_and_launch_agent", _fake_create_launch
        )
        with TestClient(app) as client:
            r = client.post("/api/schedules/draft", json={"nl": "consolidate memory nightly"})
        assert r.status_code == 200
        assert r.json()["agent_id"] == 4242
        assert calls["label"] == "ava-schedule-writer"
        prompt = str(calls["prompt"])
        assert "consolidate memory nightly" in prompt
        assert "ava.skills.ava_schedule_writer" in prompt
