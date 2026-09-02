"""Restart boot identity and record failures cannot authorize another consumer."""

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from agent._starting import claim_agent_row
from agent.restart_admission import consume_restart_command
from agent.session_admission import publish_admitted_session
from shared.runtime_incarnation import RuntimeIncarnation, current_incarnation
from tests.agent.test_lifecycle_intent import _command
from tests.agent.test_runtime_incarnation import _row


@pytest.fixture
def session_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("agent.session_admission.run_dir", lambda: tmp_path)
    return tmp_path / "sessions"


def _prepared(conn: psycopg.Connection) -> tuple[int, int]:
    agent_id = _row(conn)
    command_id = _command(conn, agent_id, "restart")
    generation, owner = uuid4(), uuid4()
    conn.execute(
        "UPDATE inbound_messages SET status='claimed',claimed_at=clock_timestamp(), "
        "applied_at=clock_timestamp(),target_generation=%s,target_owner=%s WHERE id=%s",
        (generation, owner, command_id),
    )
    conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s, "
        "runtime_kind='process',lifecycle_command_id=%s WHERE id=%s",
        (generation, owner, command_id, agent_id),
    )
    conn.commit()
    return agent_id, command_id


def test_owned_restart_does_not_fall_back_to_legacy_admission(
    db_conn: psycopg.Connection, session_directory: Path
) -> None:
    agent_id, _ = _prepared(db_conn)
    with pytest.raises(RuntimeError, match="restart admission command"):
        claim_agent_row(agent_id)
    assert not session_directory.exists()


@pytest.mark.parametrize("fault", ["command", "generation", "owner", "expired"])
def test_delayed_attempt_cannot_admit_another_target(
    db_conn: psycopg.Connection, session_directory: Path, fault: str
) -> None:
    agent_id, command_id = _prepared(db_conn)
    if fault == "command":
        command_id += 100000
    elif fault == "generation":
        db_conn.execute(
            "UPDATE agents_meta SET runtime_generation=%s WHERE id=%s", (uuid4(), agent_id)
        )
    elif fault == "owner":
        db_conn.execute("UPDATE agents_meta SET runtime_owner=%s WHERE id=%s", (uuid4(), agent_id))
    else:
        db_conn.execute(
            "UPDATE inbound_messages SET applied_at=clock_timestamp()-interval '1 day' WHERE id=%s",
            (command_id,),
        )
    db_conn.commit()
    with pytest.raises(RuntimeError, match="restart admission command"):
        claim_agent_row(agent_id, restart_command_id=command_id)
    assert not session_directory.exists()
    assert db_conn.execute(
        "SELECT pid,status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None, "idling")


@pytest.mark.parametrize("after_write", [False, True])
def test_record_failure_rolls_back_admission_and_retry_observes_once(
    db_conn: psycopg.Connection,
    session_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_write: bool,
) -> None:
    agent_id, command_id = _prepared(db_conn)

    def fail(incarnation: RuntimeIncarnation) -> None:
        if after_write:
            publish_admitted_session(incarnation)
        raise OSError("injected publication failure")

    with monkeypatch.context() as patch:
        patch.setattr("agent.session_admission.publish_admitted_session", fail)
        with pytest.raises(OSError, match="injected publication failure"):
            claim_agent_row(agent_id, restart_command_id=command_id)
    assert db_conn.execute(
        "SELECT pid,status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None, "idling")
    assert db_conn.execute(
        "SELECT observed_at FROM inbound_messages WHERE id=%s", (command_id,)
    ).fetchone() == (None,)
    claim_agent_row(agent_id, restart_command_id=command_id)
    winner = current_incarnation(agent_id)
    assert winner is not None
    records = list(session_directory.glob("*.json"))
    assert len(records) == 1
    exact = records[0].read_bytes()
    with pytest.raises(RuntimeError, match="restart admission command"):
        claim_agent_row(agent_id, restart_command_id=command_id)
    assert records[0].read_bytes() == exact
    assert db_conn.execute(
        "SELECT lifecycle_command_id,runtime_generation FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None, winner.generation)


def test_nonsecret_command_flag_removed_before_runtime_parser() -> None:
    argv = ["agent", "--restart-command-id", "123", "--agent-id", "4"]
    assert consume_restart_command(argv) == 123
    assert argv == ["agent", "--agent-id", "4"]
    assert consume_restart_command(argv) is None
