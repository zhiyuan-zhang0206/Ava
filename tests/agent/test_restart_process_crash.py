"""Only disposable CI children, using the test-owned database and artifact dir."""

import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from tests.agent.test_restart_admission import _prepared


def _boot(
    agent_id: int, command_id: int, artifacts: Path, fault: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed CI-only probe, fixture ids and tmp_path
        [
            sys.executable,
            "-m",
            "tests.agent._restart_boot_probe",
            str(agent_id),
            str(command_id),
            str(artifacts),
            fault,
        ],
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )


@pytest.mark.parametrize(
    "fault", ["before_record", "after_record", "before_commit", "after_commit"]
)
def test_process_death_at_record_commit_boundaries_never_admits_loser(
    db_conn: psycopg.Connection, tmp_path: Path, fault: str
) -> None:
    agent_id, command_id = _prepared(db_conn)
    first = _boot(agent_id, command_id, tmp_path, fault)
    assert first.returncode in {71, 72, 73, 74}, first.stderr
    assert "EXECUTION_ALLOWED" not in first.stdout
    row = db_conn.execute(
        "SELECT m.lifecycle_command_id,i.observed_at FROM agents_meta m "
        "JOIN inbound_messages i ON i.id=%s WHERE m.id=%s",
        (command_id, agent_id),
    ).fetchone()
    assert row is not None
    if fault == "after_commit":
        assert row[0] is None and row[1] is not None
    else:
        assert row == (command_id, None)
        retry = _boot(agent_id, command_id, tmp_path, "none")
        assert retry.returncode == 0, retry.stderr
        assert "EXECUTION_ALLOWED" in retry.stdout
    records = list((tmp_path / "sessions").glob("*.json"))
    assert len(records) == 1
    winner_record = records[0].read_bytes()
    late = _boot(agent_id, command_id, tmp_path, "none")
    assert late.returncode != 0 and "restart admission command" in late.stderr
    assert "EXECUTION_ALLOWED" not in late.stdout
    assert records[0].read_bytes() == winner_record
