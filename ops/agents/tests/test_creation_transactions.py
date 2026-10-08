"""Birth and task producers share the caller's actual commit/rollback boundary."""

from contextlib import nullcontext

import psycopg
import pytest

from base.agents import InvalidModelConfig
from base.agents.birth_config import set_cluster_default_model
from base.agents.tasks.creation import create_task_in_transaction
from base.agents.tasks.model import Task
from base.cluster.machine import machine_name
from base.db import Database
from ops.agents.birth_transaction import insert_agent_birth
from ops.agents.creation_identity import creation_request_hash


def _seed_parent(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO agent_tasks(title,description,status,created_by,is_root) "
        "VALUES ('Root','root','in_progress','system',true) RETURNING id"
    ).fetchone()
    assert row is not None
    conn.commit()
    return row[0]


def _counts(conn: psycopg.Connection) -> tuple[int, ...]:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM agents), (SELECT count(*) FROM agents_meta), "
        "(SELECT count(*) FROM agent_tasks), (SELECT count(*) FROM audit_events), "
        "(SELECT count(*) FROM inbound_messages)"
    ).fetchone()
    assert row is not None
    return row


def test_actual_birth_model_effort_failure_rolls_back_every_effect(
    db_conn: psycopg.Connection, cluster_defaults_unset: None
) -> None:
    before = _counts(db_conn)
    with (
        pytest.raises(InvalidModelConfig, match="unsupported reasoning effort"),
        db_conn.transaction(),
        db_conn.cursor() as cur,
    ):
        set_cluster_default_model(cur, "mimo-v2.6-pro", updated_by="test")
        insert_agent_birth(
            cur,
            machine=machine_name(),
            config={"reasoning_effort": "max"},
            prompt="This birth must roll back",
            prompt_source="user",
        )
    assert _counts(db_conn) == before


@pytest.mark.parametrize("commit", [False, True])
def test_caller_owns_birth_task_audit_and_inbound_commit(
    db_conn: psycopg.Connection, database: Database, commit: bool
) -> None:
    parent = _seed_parent(db_conn)
    with database.connect(autocommit=True) as observer:
        before = _counts(observer)
        expected = nullcontext() if commit else pytest.raises(RuntimeError, match="caller refuses")
        with expected, db_conn.transaction(), db_conn.cursor() as cur:
            actor = insert_agent_birth(cur, machine=machine_name())
            birth = insert_agent_birth(
                cur, machine=machine_name(), prompt="First instruction", prompt_source="user"
            )
            task, event, notes = create_task_in_transaction(
                cur,
                "Combined task",
                "Assignment",
                parent=parent,
                owner=birth.agent_id,
                actor=actor.agent_id,
                priority="P2",
                remind_interval_seconds=None,
            )
            assert isinstance(task, Task)
            assert event is not None and notes
            assert birth.birth_event is not None and birth.prompt_inbound_id is not None
            assert _counts(observer) == before
            inside = _counts(db_conn)
            assert all(after > earlier for after, earlier in zip(inside, before, strict=True))
            if not commit:
                raise RuntimeError("caller refuses the complete operation")
        assert _counts(observer) == (inside if commit else before)


def test_task_validation_failure_rolls_back_birth_and_prompt(
    db_conn: psycopg.Connection, database: Database
) -> None:
    _seed_parent(db_conn)
    row = db_conn.execute(
        "INSERT INTO agent_tasks(title,description,status,created_by) "
        "VALUES ('Closed parent','','done','system') RETURNING id"
    ).fetchone()
    assert row is not None
    parent = row[0]
    db_conn.commit()
    with database.connect(autocommit=True) as observer:
        before = _counts(observer)
        with (
            pytest.raises(ValueError, match="closed task cannot be"),
            db_conn.transaction(),
            db_conn.cursor() as cur,
        ):
            birth = insert_agent_birth(
                cur, machine=machine_name(), prompt="First instruction", prompt_source="user"
            )
            create_task_in_transaction(
                cur,
                "Rejected task",
                "",
                parent=parent,
                owner=birth.agent_id,
                actor=birth.agent_id,
                priority="P2",
                remind_interval_seconds=None,
            )
        assert _counts(observer) == before


def test_fleet_task_model_remains_the_shared_model() -> None:
    from ava_builtins.plugins.ava_fleet.task_registry import Task as FleetTask

    assert FleetTask is Task


def test_cursor_birth_replay_returns_original_attempt_without_new_effects(
    db_conn: psycopg.Connection,
) -> None:
    with db_conn.transaction(), db_conn.cursor() as cur:
        original = insert_agent_birth(
            cur,
            machine=machine_name(),
            prompt="First instruction",
            prompt_source="user",
            creation_key="transaction-birth",
            creation_request_hash=creation_request_hash({"prompt": "First instruction"}),
        )
    before = _counts(db_conn)
    db_conn.commit()
    with db_conn.transaction(), db_conn.cursor() as cur:
        replay = insert_agent_birth(
            cur,
            machine=machine_name(),
            prompt="First instruction",
            prompt_source="user",
            creation_key="transaction-birth",
            creation_request_hash=creation_request_hash({"prompt": "First instruction"}),
        )
        assert replay.agent_id == original.agent_id
        assert replay.launch_attempt_id == original.launch_attempt_id
        assert replay.birth_config == original.birth_config
        assert replay.birth_event is None
        assert replay.prompt_event is None and replay.prompt_inbound_id is None
        assert _counts(db_conn) == before
