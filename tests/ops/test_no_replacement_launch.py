"""Normal boot and durable restart never replace a resident by name."""

import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import psutil
import psycopg
import pytest

from agent._starting import claim_agent_row
from ops import agent_launch, agent_wake
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


@pytest.mark.real_agent_launch
def test_confirm_tracks_returned_attempt_before_canonical_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = Mock()
    backend.new_session.return_value = True

    def live_attempt(name: str) -> bool:
        return name.startswith("ava-boot-")

    backend.has_session.side_effect = live_attempt
    monkeypatch.setattr(agent_launch, "native_proc", Mock(return_value=backend))
    monkeypatch.setattr(agent_launch, "agent_spawn_env_dict", Mock(return_value={}))
    confirm = Mock()
    monkeypatch.setattr(agent_launch, "_wait_for_agent_claim", confirm)
    attempt = agent_launch._launch_agent_process(123)
    confirm.assert_called_once_with(123, attempt)
    assert agent_launch._launched_process_alive(123, attempt)
    backend.has_session.assert_called_once_with(attempt)
    backend.has_session.return_value = False
    backend.has_session.side_effect = None
    assert not agent_launch._launched_process_alive(123, attempt)


def test_postcommit_resurrect_confirmation_preserves_exact_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = agent_wake._PreparedResurrect(
        datetime.now(UTC), None, None, None, "ava-boot-123-exact"
    )
    confirm = Mock()
    monkeypatch.setattr(agent_launch, "_wait_for_agent_claim", confirm)
    assert agent_wake._confirm_resurrect_with_retries(123, prepared, first_attempt=0)
    confirm.assert_called_once_with(123, prepared.attempt_session)


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
