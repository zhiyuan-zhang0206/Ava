"""Normal boot and durable restart never replace a resident by name."""

import os
from pathlib import Path
from unittest.mock import Mock

import psutil
import psycopg
import pytest

from agent._starting import claim_agent_row
from ops import agent_launch
from shared.session_record import SessionRecord
from tests.agent.test_runtime_incarnation import _row


@pytest.mark.real_agent_launch
def test_normal_launch_allocates_unique_attempt_records(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = Mock()
    backend.new_session.return_value = True
    monkeypatch.setattr(agent_launch, "native_proc", Mock(return_value=backend))
    monkeypatch.setattr(agent_launch, "agent_spawn_env_dict", Mock(return_value={}))
    for _ in range(2):
        agent_launch._launch_agent_process(123, confirm=False)
    names = [call.args[0] for call in backend.new_session.call_args_list]
    assert len(set(names)) == 2 and all(name.startswith("ava-boot-123-") for name in names)
    backend.kill_session.assert_not_called()


def test_ordinary_admission_alone_publishes_canonical(
    db_conn: psycopg.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("agent.session_admission.run_dir", lambda: tmp_path)
    agent_id = _row(db_conn)
    claim_agent_row(agent_id)
    record = SessionRecord.read(tmp_path / "sessions" / f"ava-agent-{agent_id}.json")
    assert record is not None and record.pid == os.getpid()
    row = db_conn.execute(
        "SELECT runtime_generation FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    assert row is not None and record.generation == str(row[0])


@pytest.mark.parametrize("unknown_birth", [False, True])
def test_resident_canonical_refuses_without_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unknown_birth: bool
) -> None:
    monkeypatch.setattr(agent_launch, "run_dir", lambda: tmp_path)
    process = psutil.Process(os.getpid())
    path = tmp_path / "sessions" / "ava-agent-123.json"
    SessionRecord(
        process.pid, 0 if unknown_birth else process.create_time(), "resident", "/", 0
    ).write(path)
    before = path.read_bytes()
    backend = Mock()
    monkeypatch.setattr(agent_launch, "native_proc", Mock(return_value=backend))
    with pytest.raises(RuntimeError, match="still live"):
        agent_launch._require_released_agent_session(123)
    backend.kill_session.assert_not_called()
    assert path.read_bytes() == before and process.is_running()


def test_unreadable_canonical_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_launch, "run_dir", lambda: tmp_path)
    path = tmp_path / "sessions" / "ava-agent-123.json"
    path.parent.mkdir()
    path.write_text("{")
    with pytest.raises(RuntimeError, match="unreadable"):
        agent_launch._require_released_agent_session(123)
