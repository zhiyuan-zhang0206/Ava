"""Schedule management through the real gateway and the installed ``ava`` CLI.

The e2e stack has no schedule-manager. Control requests therefore leave durable
sync rows for its later consumer; session execution belongs to SC2/SC3.
"""

from __future__ import annotations

import subprocess
import sys

import httpx
import psycopg
import pytest

from base.cluster import session_name
from base.config import settings
from base.sessions.backend import get_shell_backend
from tests.components.base.poll_until import poll_until
from tests.e2e._ports import GATEWAY_URL
from tests.e2e.fakes.scenario_recording import model_inputs, reset_record


def _cli(*args: str) -> str:
    result = subprocess.run(  # noqa: S603 -- fixed Python executable and CLI module
        [sys.executable, "-m", "cli", "schedules", *args],
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result.stdout


def _create_and_check_listed(client: httpx.Client) -> int:
    created = client.post(
        "/api/schedules",
        json={"name": "e2e-rest", "script": "print('first')\n", "enabled": False},
    )
    assert created.status_code == 201, created.text
    schedule_id = int(created.json()["id"])
    assert created.json()["script"] == "print('first')\n"
    assert created.json()["status"] == "stopped"
    listed = client.get("/api/schedules")
    listed.raise_for_status()
    assert [row["id"] for row in listed.json()] == [schedule_id]
    assert "script" not in listed.json()[0]
    assert client.get(f"/api/schedules/{schedule_id}").json()["script"] == "print('first')\n"
    return schedule_id


@pytest.mark.scenario("tests.e2e.fakes.scenarios.schedules:build")
def test_rest_create_list_get_update_delete_and_version_snapshot(
    gateway_proc: str, truncated_db: None
) -> None:
    with httpx.Client(base_url=GATEWAY_URL, timeout=30.0) as client:
        schedule_id = _create_and_check_listed(client)

        bad = client.put(f"/api/schedules/{schedule_id}", json={"script": "def (:\n"})
        assert bad.status_code == 400 and "syntax error" in bad.json()["detail"]
        updated = client.put(
            f"/api/schedules/{schedule_id}",
            json={"name": "e2e-renamed", "description": "edited", "script": "print('second')\n"},
        )
        updated.raise_for_status()
        assert updated.json()["description"] == "edited"
        assert updated.json()["script"] == "print('second')\n"
        assert client.get(f"/api/schedules/{schedule_id}").json()["name"] == "e2e-renamed"
        assert (
            client.post(
                "/api/schedules", json={"name": "e2e-renamed", "script": "pass\n"}
            ).status_code
            == 409
        )

        with psycopg.connect(settings.data_plane.db_url) as conn:
            versions = conn.execute(
                "SELECT note, script FROM schedule_versions WHERE schedule_id = %s ORDER BY id",
                (schedule_id,),
            ).fetchall()
        assert versions == [("initial", "print('first')\n"), ("edit", "print('second')\n")]

        deleted = client.delete(f"/api/schedules/{schedule_id}")
        assert deleted.status_code == 200, deleted.text
        assert client.get(f"/api/schedules/{schedule_id}").status_code == 404
        assert client.get("/api/schedules").json() == []
        with psycopg.connect(settings.data_plane.db_url) as conn:
            assert conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [
                (schedule_id,)
            ]


@pytest.mark.scenario("tests.e2e.fakes.scenarios.schedules:build")
def test_rest_start_stop_restart_queue_sync_and_change_enabled(
    gateway_proc: str, truncated_db: None
) -> None:
    with httpx.Client(base_url=GATEWAY_URL, timeout=30.0) as client:
        created = client.post(
            "/api/schedules", json={"name": "e2e-control", "script": "pass\n", "enabled": False}
        )
        created.raise_for_status()
        schedule_id = created.json()["id"]
        base = f"/api/schedules/{schedule_id}"
        assert client.post(f"{base}/restart").status_code == 409
        started = client.post(f"{base}/start")
        started.raise_for_status()
        assert started.json()["enabled"] is True
        restarted = client.post(f"{base}/restart")
        restarted.raise_for_status()
        assert restarted.json()["enabled"] is True
        stopped = client.post(f"{base}/stop")
        stopped.raise_for_status()
        assert stopped.json()["enabled"] is False
        assert client.get(base).json()["enabled"] is False
        with psycopg.connect(settings.data_plane.db_url) as conn:
            assert conn.execute("SELECT schedule_id FROM schedule_sync_requests").fetchall() == [
                (schedule_id,)
            ]


@pytest.mark.scenario("tests.e2e.fakes.scenarios.schedules:build")
def test_rest_runs_and_logs_read_persisted_history_and_transcript(
    gateway_proc: str, truncated_db: None, pty_sessions_proc: None
) -> None:
    with httpx.Client(base_url=GATEWAY_URL, timeout=30.0) as client:
        created = client.post(
            "/api/schedules", json={"name": "e2e-history", "script": "pass\n", "enabled": False}
        )
        created.raise_for_status()
        schedule_id = created.json()["id"]
        base = f"/api/schedules/{schedule_id}"
        assert client.get(f"{base}/runs").json() == []
        assert client.get(f"{base}/logs").json() == {"source": "none", "lines": []}

        with psycopg.connect(settings.data_plane.db_url) as conn:
            old_id = conn.execute(
                "INSERT INTO schedule_runs (schedule_id, ok, note) "
                "VALUES (%s, TRUE, 'first run') RETURNING id",
                (schedule_id,),
            ).fetchone()
            new_id = conn.execute(
                "INSERT INTO schedule_runs (schedule_id, ok, note) "
                "VALUES (%s, FALSE, 'second run') RETURNING id",
                (schedule_id,),
            ).fetchone()
            conn.commit()
        assert old_id is not None and new_id is not None
        runs = client.get(f"{base}/runs?limit=1")
        runs.raise_for_status()
        assert [(row["id"], row["ok"], row["note"]) for row in runs.json()] == [
            (new_id[0], False, "second run")
        ]
        assert [row["id"] for row in client.get(f"{base}/runs").json()] == [new_id[0], old_id[0]]

        transcript = get_shell_backend().session_log_path(session_name(f"schedule-{schedule_id}"))
        assert transcript is not None
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("first line\nsecond line\n")
        logs = client.get(f"{base}/logs?lines=1")
        logs.raise_for_status()
        assert logs.json() == {"source": "transcript", "lines": ["second line"]}
        assert client.get("/api/schedules/999999999/runs").status_code == 404


@pytest.mark.scenario("tests.e2e.fakes.scenarios.schedules:build")
def test_draft_launches_a_writer_with_the_request_in_model_input(spawned_agent: int) -> None:
    reset_record()
    request_text = "e2e draft consolidate memory each night"
    response = httpx.post(
        f"{GATEWAY_URL}/api/schedules/draft", json={"nl": request_text}, timeout=90.0
    )
    response.raise_for_status()
    writer_id = int(response.json()["agent_id"])
    assert writer_id != spawned_agent
    try:
        poll_until(
            lambda: (bool(model_inputs(writer_id)), model_inputs(writer_id)),
            timeout=60.0,
            interval=0.3,
            what="draft writer receives its first model input",
        )
        human = "\n".join(
            message["text"] for message in model_inputs(writer_id)[0] if message["type"] == "human"
        )
        assert request_text in human
        assert "ava.skills.ava_guide.schedules" in human
        with psycopg.connect(settings.data_plane.db_url) as conn:
            row = conn.execute("SELECT label FROM agents WHERE id = %s", (writer_id,)).fetchone()
        assert row == ("ava-schedule-writer",)
    finally:
        httpx.post(f"{GATEWAY_URL}/api/agents/{writer_id}/terminate", timeout=10.0)


@pytest.mark.scenario("tests.e2e.fakes.scenarios.schedules:build")
def test_cli_schedules_manage_the_same_gateway_rows(
    gateway_proc: str, truncated_db: None, pty_sessions_proc: None
) -> None:
    assert "created schedule" in _cli(
        "create", "--name", "e2e-cli", "--script", "print('cli')", "--disabled"
    )
    assert "e2e-cli" in _cli("ls")
    assert "print('cli')" in _cli("get", "e2e-cli")
    assert "updated schedule" in _cli("update", "e2e-cli", "--description", "from cli")
    assert "from cli" in _cli("get", "e2e-cli")
    assert "(no runs yet)" in _cli("runs", "e2e-cli")
    assert "(no output yet" in _cli("logs", "e2e-cli")
    assert "enabled=True" in _cli("start", "e2e-cli")
    assert "restart" in _cli("restart", "e2e-cli")
    assert "enabled=False" in _cli("stop", "e2e-cli")
    assert "deleted schedule" in _cli("delete", "e2e-cli", "--force")
    assert "e2e-cli" not in _cli("ls")
