"""Generic cleanup cannot overwrite a durable lifecycle decision."""

from unittest.mock import Mock

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agent._starting import _mark_preclaim_terminated, claim_agent_row
from ops import agent_launch
from services.delivery_watchdog.daemon import dead_letter_stale_claimed
from shared.db import insert_inbound_message
from tests.agent.test_restart_admission import _prepared
from tests.agent.test_runtime_incarnation import _row
from tests.ops.test_cold_lifecycle import (
    sync_pool as cold_pool,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)


@pytest.mark.parametrize("wrong_host", [False, True])
def test_rejected_boot_cannot_terminate_prepared_owned_target(
    db_conn: psycopg.Connection, wrong_host: bool
) -> None:
    agent_id, command_id = _prepared(db_conn)
    if wrong_host:
        db_conn.execute("UPDATE agents_meta SET machine='other-host' WHERE id=%s", (agent_id,))
        db_conn.commit()
        with pytest.raises(RuntimeError, match="placement mismatch"):
            claim_agent_row(agent_id, restart_command_id=command_id)
    else:
        _mark_preclaim_terminated(agent_id)
    assert db_conn.execute(
        "SELECT status,pid,lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling", None, command_id)


def test_legacy_unowned_boot_failure_retains_terminal_contract(db_conn: psycopg.Connection) -> None:
    agent_id = _row(db_conn)
    _mark_preclaim_terminated(agent_id)
    assert db_conn.execute(
        "SELECT status,termination_source FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("terminated", "launch-confirm")


def test_generic_dead_letter_leaves_fixed_command_claimed(
    db_conn: psycopg.Connection,
    cold_pool: ConnectionPool,  # noqa: F811
) -> None:
    agent_id, command_id = _prepared(db_conn)
    chat_id = insert_inbound_message(db_conn, agent_id, "old chat", "cli")
    db_conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='exit' WHERE id=%s",
        (agent_id,),
    )
    db_conn.execute(
        "UPDATE inbound_messages SET status='claimed',claimed_at=clock_timestamp()-interval '1 day' "
        "WHERE agent_id=%s",
        (agent_id,),
    )
    db_conn.commit()
    assert dead_letter_stale_claimed(cold_pool, 60, 7200.0) == 1
    assert db_conn.execute(
        "SELECT id,status FROM inbound_messages WHERE agent_id=%s ORDER BY id", (agent_id,)
    ).fetchall() == [(command_id, "claimed"), (chat_id, "done")]
    assert db_conn.execute(
        "SELECT lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == (command_id,)


@pytest.mark.parametrize("retry", [False, True])
@pytest.mark.parametrize("owned", [False, True])
def test_delayed_launcher_cleanup_never_settles_new_owned_command(
    db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch, retry: bool, owned: bool
) -> None:
    agent_id, command_id = _prepared(db_conn) if owned else (_row(db_conn), None)
    failure = Mock(side_effect=RuntimeError("injected old launch timeout"))
    if retry:
        monkeypatch.setattr(agent_launch, "_LAUNCH_MAX_RETRIES", 0)
        monkeypatch.setattr(agent_launch, "_launch_agent_process", failure)
        with pytest.raises(RuntimeError, match="old launch timeout"):
            agent_launch._launch_or_force_terminated(agent_id)
    else:
        monkeypatch.setattr(agent_launch, "_wait_for_agent_claim", failure)
        assert not agent_launch._confirm_launch_or_force_terminated(agent_id)
    assert db_conn.execute(
        "SELECT status,pid,lifecycle_command_id FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling" if owned else "terminated", None, command_id)
