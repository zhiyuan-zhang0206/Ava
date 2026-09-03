"""Actual controller entry preserves command identity across repeated crashes."""

from unittest.mock import Mock

import psycopg
import pytest

from ops import agent_launch
from ops.agent_identity import AgentProcessIdentity
from ops.agent_wake import respawn_agent
from ops.lifecycle_recovery import RestartAttempt, _authorize_attempt
from shared.db import insert_inbound_message
from tests.agent.test_restart_admission import _prepared


@pytest.fixture
def owned_restart(db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    agent_id, command_id = _prepared(db_conn)
    db_conn.execute("UPDATE inbound_messages SET payload=NULL WHERE id=%s", (command_id,))
    db_conn.execute("UPDATE agents_meta SET status='restarting',pid=12345 WHERE id=%s", (agent_id,))
    db_conn.commit()
    monkeypatch.setattr(
        "ops.lifecycle_recovery.probe_agent_process", Mock(return_value=AgentProcessIdentity.GONE)
    )
    backend = Mock()
    backend.has_session.return_value = False
    monkeypatch.setattr("ops.lifecycle_recovery.native_proc", Mock(return_value=backend))
    return agent_id, command_id


def test_controller_crash_after_authorization_never_resets_attempt_limit(
    db_conn: psycopg.Connection, owned_restart: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_id, command_id = owned_restart
    spawn = Mock(side_effect=RuntimeError("crash after authorization before spawn"))
    monkeypatch.setattr(agent_launch, "_launch_agent_process", spawn)
    for _ in range(agent_launch._LAUNCH_MAX_RETRIES + 1):
        with pytest.raises(RuntimeError, match="crash after authorization"):
            respawn_agent(agent_id)
    for _ in range(5):
        assert not respawn_agent(agent_id)
    assert spawn.call_count == agent_launch._LAUNCH_MAX_RETRIES + 1
    row = db_conn.execute(
        "SELECT payload,observed_at,status FROM inbound_messages WHERE id=%s", (command_id,)
    ).fetchone()
    assert row is not None
    assert row[0]["launch_attempts"] == agent_launch._LAUNCH_MAX_RETRIES + 1
    assert row[0]["lifecycle_result"] == {
        "outcome": "unobserved",
        "reason": "launch_attempts_exhausted",
    }
    assert row[1:] == (None, "claimed")
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (command_id,)


@pytest.mark.parametrize("identity", [AgentProcessIdentity.OWNED, AgentProcessIdentity.UNREADABLE])
def test_live_or_unknown_original_process_never_allocates(
    db_conn: psycopg.Connection,
    owned_restart: tuple[int, int],
    monkeypatch: pytest.MonkeyPatch,
    identity: AgentProcessIdentity,
) -> None:
    agent_id, command_id = owned_restart
    monkeypatch.setattr("ops.lifecycle_recovery.probe_agent_process", Mock(return_value=identity))
    assert _authorize_attempt(agent_id) is False
    assert db_conn.execute(
        "SELECT payload FROM inbound_messages WHERE id=%s", (command_id,)
    ).fetchone() == (None,)


def test_expired_command_fails_without_successful_observation(
    db_conn: psycopg.Connection, owned_restart: tuple[int, int]
) -> None:
    agent_id, command_id = owned_restart
    db_conn.execute(
        "UPDATE inbound_messages SET applied_at=clock_timestamp()-interval '1 day' WHERE id=%s",
        (command_id,),
    )
    db_conn.commit()
    assert _authorize_attempt(agent_id) is False
    row = db_conn.execute(
        "SELECT payload FROM inbound_messages WHERE id=%s", (command_id,)
    ).fetchone()
    assert row is not None and "launch_attempts" not in row[0]
    assert row[0]["lifecycle_result"] == {"outcome": "failed", "reason": "restart_deadline_expired"}
    assert db_conn.execute(
        "SELECT status,observed_at FROM inbound_messages WHERE id=%s", (command_id,)
    ).fetchone() == ("done", None)
    assert db_conn.execute(
        "SELECT status,lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated", None)


def test_original_application_time_and_target_survive_prepare_idling_crash(
    db_conn: psycopg.Connection, owned_restart: tuple[int, int]
) -> None:
    agent_id, command_id = owned_restart
    before = db_conn.execute(
        "SELECT target_generation,target_owner,claimed_at,applied_at FROM inbound_messages WHERE id=%s",
        (command_id,),
    ).fetchone()
    first = _authorize_attempt(agent_id)
    second = _authorize_attempt(agent_id)
    assert isinstance(first, RestartAttempt) and isinstance(second, RestartAttempt)
    assert (first.command_id, first.number, second.command_id, second.number) == (
        command_id,
        1,
        command_id,
        2,
    )
    assert second.remaining_budget <= first.remaining_budget
    assert (
        db_conn.execute(
            "SELECT target_generation,target_owner,claimed_at,applied_at FROM inbound_messages WHERE id=%s",
            (command_id,),
        ).fetchone()
        == before
    )


def test_external_producer_cannot_supply_launch_counter(
    db_conn: psycopg.Connection, owned_restart: tuple[int, int]
) -> None:
    with pytest.raises(ValueError, match="launch_attempts is reserved"):
        insert_inbound_message(
            db_conn, owned_restart[0], "", "cli", kind="restart", payload={"launch_attempts": 0}
        )


@pytest.mark.real_agent_launch
def test_actual_launcher_never_touches_canonical_session_for_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = Mock()
    backend.new_session.return_value = True
    monkeypatch.setattr(agent_launch, "native_proc", Mock(return_value=backend))
    monkeypatch.setattr(agent_launch, "agent_spawn_env_dict", Mock(return_value={}))
    agent_launch._launch_agent_process(123, confirm=False, restart_attempt=(456, 2, 8.0))
    backend.kill_session.assert_not_called()
    call = backend.new_session.call_args
    assert call.args[0] == "ava-boot-123-456-2"
    argv = call.args[1]
    assert argv[argv.index("--restart-command-id") + 1] == "456"
    assert float(argv[argv.index("--boot-budget-seconds") + 1]) == 8.0


@pytest.mark.parametrize("counter", [-1, True, "1", 999])
def test_malformed_internal_counter_fails_before_launch(
    db_conn: psycopg.Connection, owned_restart: tuple[int, int], counter: object
) -> None:
    from psycopg.types.json import Jsonb

    db_conn.execute(
        "UPDATE inbound_messages SET payload=%s WHERE id=%s",
        (Jsonb({"launch_attempts": counter}), owned_restart[1]),
    )
    db_conn.commit()
    with pytest.raises(ValueError, match="invalid reserved launch_attempts"):
        _authorize_attempt(owned_restart[0])


def test_existing_respawn_entry_uses_owned_command_recovery(
    owned_restart: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    recovery = Mock(return_value=True)
    monkeypatch.setattr("ops.lifecycle_recovery.recover_lifecycle_command", recovery)
    assert respawn_agent(owned_restart[0])
    recovery.assert_called_once_with(owned_restart[0])
