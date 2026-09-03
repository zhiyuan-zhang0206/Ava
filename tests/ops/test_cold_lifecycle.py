"""A new explicit lifecycle command, never chat/history, can recover a failed boot."""

from collections.abc import Iterator
from pathlib import Path
from typing import Literal
from unittest.mock import Mock

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from ops import agent_launch
from ops.agent_identity import AgentProcessIdentity
from ops.agent_wake import respawn_agent, resurrect_agent
from ops.ops_exit import _force_terminate_transaction
from ops.ops_lifecycle import _restart_blocking
from ops.rpc_schemas import RestartAgentRequest
from shared.config import settings
from shared.db import PG_KEEPALIVE_KWARGS, insert_inbound_message
from shared.machine import machine_name
from tests.agent.test_lifecycle_intent import _command
from tests.agent.test_restart_admission import _prepared
from tests.agent.test_restart_process_crash import _boot


@pytest.fixture
def sync_pool() -> Iterator[ConnectionPool]:
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=2, kwargs=PG_KEEPALIVE_KWARGS
    ) as pool:
        yield pool


def _expire(conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int, Mock]:
    agent_id, command_id = _prepared(conn)
    conn.execute("UPDATE agents_meta SET pid=12345,status='restarting' WHERE id=%s", (agent_id,))
    conn.execute(
        "UPDATE inbound_messages SET applied_at=clock_timestamp()-interval '1 day',"
        "payload=payload||%s WHERE id=%s",
        (
            Jsonb(
                {
                    "target_process_identity": {
                        "machine": machine_name(),
                        "pid": 12345,
                        "create_time": 1.0,
                        "starttime": None,
                    }
                }
            ),
            command_id,
        ),
    )
    conn.commit()
    monkeypatch.setattr(
        "ops.lifecycle_recovery.probe_agent_process", Mock(return_value=AgentProcessIdentity.GONE)
    )
    native = Mock()
    native.has_session.return_value = False
    monkeypatch.setattr("ops.lifecycle_recovery.native_proc", Mock(return_value=native))
    launch = Mock()
    monkeypatch.setattr(agent_launch, "_launch_agent_process", launch)
    assert not respawn_agent(agent_id)
    assert conn.execute(
        "SELECT status,observed_at FROM inbound_messages WHERE id=%s", (command_id,)
    ).fetchone() == ("done", None)
    return agent_id, command_id, launch


@pytest.mark.parametrize("identity", ["missing", "unverified"])
def test_cold_command_without_positive_exact_absence_remains_pending(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, identity: str
) -> None:
    agent_id, old, launch = _expire(db_conn, monkeypatch)
    if identity == "missing":
        db_conn.execute(
            "UPDATE inbound_messages SET payload=payload-'target_process_identity' WHERE id=%s",
            (old,),
        )
        db_conn.commit()
    else:
        monkeypatch.setattr("ops.cold_lifecycle.target_process_ended", Mock(return_value=False))
    command = _command(db_conn, agent_id, "terminate")
    assert not respawn_agent(agent_id)
    launch.assert_not_called()
    assert db_conn.execute(
        "SELECT status,claimed_at,applied_at,observed_at FROM inbound_messages WHERE id=%s",
        (command,),
    ).fetchone() == ("pending", None, None, None)
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)


def test_failed_command_then_explicit_restart_admits_one_real_consumer(
    db_conn: psycopg.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sync_pool: ConnectionPool,
) -> None:
    agent_id, old_command, launch = _expire(db_conn, monkeypatch)
    next_command = _restart_blocking(agent_id, RestartAgentRequest(source="cli"), sync_pool)
    assert next_command is not None
    assert respawn_agent(agent_id)
    launch.assert_called_once()
    assert launch.call_args.kwargs["restart_attempt"][:2] == (next_command, 1)
    winner = _boot(agent_id, next_command, tmp_path, "none")
    assert winner.returncode == 0, winner.stderr
    assert "EXECUTION_ALLOWED" in winner.stdout
    late = _boot(agent_id, old_command, tmp_path, "none")
    assert late.returncode != 0 and "EXECUTION_ALLOWED" not in late.stdout
    assert "restart admission command" in late.stderr
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (next_command,)
    ).fetchone() == ("done", True)
    assert db_conn.execute(
        "SELECT payload->'lifecycle_result'->>'outcome',observed_at FROM inbound_messages WHERE id=%s",
        (old_command,),
    ).fetchone() == ("failed", None)


def test_real_exit_then_cold_terminate_and_explicit_resurrect(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    agent_id, initial = _prepared(db_conn)
    child = _boot(agent_id, initial, tmp_path, "apply-restart-exit")
    assert child.returncode == 0, child.stderr
    assert "RESTART_APPLIED" in child.stdout
    row = db_conn.execute(
        "SELECT lifecycle_command_id,pid FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    assert row is not None and row[0] is not None and row[1] is not None
    failed, old_pid = row
    db_conn.execute(
        "UPDATE inbound_messages SET applied_at=clock_timestamp()-interval '1 day' WHERE id=%s",
        (failed,),
    )
    db_conn.commit()
    assert not respawn_agent(agent_id)
    stop = _command(db_conn, agent_id, "terminate")
    assert not respawn_agent(agent_id)
    assert db_conn.execute(
        "SELECT status,applied_at IS NOT NULL,observed_at IS NOT NULL "
        "FROM inbound_messages WHERE id=%s",
        (stop,),
    ).fetchone() == ("done", True, True)
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (None,)
    # Use the real preparer/authorization and a disposable child for the exact
    # returned identity; the parent test never starts a long-lived agent.
    attempts: list[int] = []

    def launch(*args: object, **kwargs: object) -> str:
        from typing import cast

        value = kwargs["resurrect_attempt"]
        assert isinstance(value, tuple)
        command: object = cast(tuple[object, ...], value)[0]
        assert type(command) is int
        attempts.append(command)
        return "test-owned-cold-attempt"

    monkeypatch.setattr(agent_launch, "_launch_agent_process", launch)
    resurrect_agent(agent_id, resurrected_by="user", prompt="after cold terminate")
    assert len(attempts) == 1
    successor = _boot(agent_id, attempts[0], tmp_path, "resurrect")
    assert successor.returncode == 0, successor.stderr
    assert "EXECUTION_ALLOWED" in successor.stdout
    assert db_conn.execute(
        "SELECT pid<>%s FROM agents_meta WHERE id=%s", (old_pid, agent_id)
    ).fetchone() == (True,)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE agent_id=%s AND content='after cold terminate'",
        (agent_id,),
    ).fetchone() == ("claimed",)


def test_force_supersedes_old_restart_but_preserves_later_explicit_command(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sync_pool: ConnectionPool,
) -> None:
    agent_id, initial = _prepared(db_conn)
    child = _boot(agent_id, initial, tmp_path, "apply-restart-exit")
    assert child.returncode == 0, child.stderr
    before = db_conn.execute(
        "SELECT i.id,i.target_generation,i.target_owner,i.applied_at "
        "FROM agents_meta m JOIN inbound_messages i ON i.id=m.lifecycle_command_id "
        "WHERE m.id=%s",
        (agent_id,),
    ).fetchone()
    assert before is not None
    queued_old = _command(db_conn, agent_id, "restart")
    chat = insert_inbound_message(db_conn, agent_id, "preserve ordinary chat", "user")
    db_conn.commit()
    _, _, _, force = _force_terminate_transaction(
        agent_id, sync_pool, source="user", kill_process=False
    )
    later = _command(db_conn, agent_id, "restart")
    assert db_conn.execute(
        "SELECT status,payload->'lifecycle_result'->>'reason',observed_at "
        "FROM inbound_messages WHERE id IN (%s,%s) ORDER BY id",
        (before[0], queued_old),
    ).fetchall() == [("done", "force_terminate", None), ("done", "force_terminate", None)]
    assert (
        db_conn.execute(
            "SELECT id,target_generation,target_owner,applied_at FROM inbound_messages WHERE id=%s",
            (before[0],),
        ).fetchone()
        == before
    )
    late = _boot(agent_id, before[0], tmp_path, "none")
    assert late.returncode != 0 and "EXECUTION_ALLOWED" not in late.stdout
    assert "restart admission command" in late.stderr
    launched = Mock(return_value="test-owned-force-successor")
    monkeypatch.setattr(agent_launch, "_launch_agent_process", launched)
    assert not respawn_agent(agent_id)  # discharge the force, not the old restart
    launched.assert_not_called()
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (force,)
    ).fetchone() == ("done", True)
    assert db_conn.execute(
        "SELECT id,status FROM inbound_messages WHERE id IN (%s,%s) ORDER BY id", (chat, later)
    ).fetchall() == [(chat, "pending"), (later, "pending")]
    assert respawn_agent(agent_id)
    launched.assert_called_once()
    assert launched.call_args.kwargs["restart_attempt"][:2] == (later, 1)
    successor = _boot(agent_id, later, tmp_path, "none")
    assert successor.returncode == 0, successor.stderr
    assert db_conn.execute(
        "SELECT status,observed_at IS NOT NULL FROM inbound_messages WHERE id=%s", (later,)
    ).fetchone() == ("done", True)


@pytest.mark.parametrize("followup", ["none", "chat", "terminate"])
def test_cold_state_never_implicitly_revives_history_or_chat(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, followup: str
) -> None:
    agent_id, _, launch = _expire(db_conn, monkeypatch)
    message = None
    if followup == "chat":
        message = insert_inbound_message(db_conn, agent_id, "not a lifecycle command", "cli")
        db_conn.commit()
    elif followup == "terminate":
        message = _command(db_conn, agent_id, "terminate")
    for _ in range(3):
        assert not respawn_agent(agent_id)
    launch.assert_not_called()
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated",)
    if message is not None:
        expected = "pending" if followup == "chat" else "done"
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (message,)
        ).fetchone() == (expected,)


def test_watchdog_and_final_pending_work_cas_refuse_failed_restart_chat(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, sync_pool: ConnectionPool
) -> None:
    from ops.agent_wake import (
        ResurrectTriggerStaleError,
        _transition_terminated_to_unclaimed_idling,
    )
    from services.delivery_watchdog.daemon import select_terminated_owners_with_pending
    from shared.db_transaction import write_transaction

    agent_id, _, launch = _expire(db_conn, monkeypatch)
    chat = insert_inbound_message(db_conn, agent_id, "must stay queued", "cli")
    db_conn.commit()
    assert (agent_id, chat) not in select_terminated_owners_with_pending(sync_pool)
    with (
        pytest.raises(ResurrectTriggerStaleError),
        write_transaction(sync_pool) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
        _transition_terminated_to_unclaimed_idling(
            cur, agent_id, trigger_inbound_id=chat, trigger_inbound_kind="chat", auto_claim=None
        )
    launch.assert_not_called()
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated",)


@pytest.mark.parametrize("kind", ["chat", "compact_request", "system_note"])
def test_normal_terminated_owner_keeps_existing_pending_work_wake_contract(
    db_conn: psycopg.Connection,
    sync_pool: ConnectionPool,
    kind: Literal["chat", "compact_request", "system_note"],
) -> None:
    from ops.agent_wake import _transition_terminated_to_unclaimed_idling
    from services.delivery_watchdog.daemon import select_terminated_owners_with_pending
    from shared.db_transaction import write_transaction

    agent_id, command_id = _prepared(db_conn)
    db_conn.execute(
        "UPDATE inbound_messages SET status='done',observed_at=clock_timestamp() WHERE id=%s",
        (command_id,),
    )
    db_conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='exit',pid=NULL, "
        "lease_expires_at=NULL,lifecycle_command_id=NULL WHERE id=%s",
        (agent_id,),
    )
    db_conn.commit()
    message = insert_inbound_message(db_conn, agent_id, "existing wake contract", "cli", kind=kind)
    db_conn.commit()
    selected = select_terminated_owners_with_pending(sync_pool)
    assert ((agent_id, message) in selected) == (kind == "chat")
    with write_transaction(sync_pool) as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
        _transition_terminated_to_unclaimed_idling(
            cur, agent_id, trigger_inbound_id=message, trigger_inbound_kind=kind, auto_claim=None
        )
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)
