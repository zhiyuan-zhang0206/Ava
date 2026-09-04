"""Real metadata locking serializes complete exec registration and force freeze."""

import threading
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from shared.db import create_agent
from shared.hosted_force import install_hosted_force
from shared.incarnation_resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceBirth,
    ResourceEvidenceError,
    ResourceProcess,
    admit_first_resources,
    attach_exec,
    decode_resources,
    freeze_resources,
    register_exec,
)
from shared.runtime_incarnation import RuntimeIncarnation


def _admitted(conn: psycopg.Connection) -> RuntimeIncarnation:
    target = RuntimeIncarnation(create_agent(conn), uuid4(), uuid4())
    birth = ResourceBirth(birth=uuid4())
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine,incarnation_resources) "
        "VALUES(%s,'idling','resource-test',%s)",
        (target.agent_id, Jsonb(birth.model_dump(mode="json"))),
    )
    admit_first_resources(conn, target, birth.birth)
    conn.execute(
        "UPDATE agents_meta SET runtime_generation=%s,runtime_owner=%s,runtime_kind='hosted',"
        "lease_expires_at=clock_timestamp()+interval '1 minute' WHERE id=%s",
        (target.generation, target.owner, target.agent_id),
    )
    conn.commit()
    return target


def _entry() -> ExecAllocation:
    return ExecAllocation(
        request=uuid4(),
        domain=uuid4(),
        request_digest="a" * 64,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )


def _force(conn: psycopg.Connection, target: RuntimeIncarnation) -> int:
    conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (target.agent_id,))
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,content,source,status) "
        "VALUES(%s,'terminate','','user','pending') RETURNING id",
        (target.agent_id,),
    ).fetchone()
    assert row is not None
    conn.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user',"
        "lifecycle_command_id=%s WHERE id=%s",
        (row[0], target.agent_id),
    )
    install_hosted_force(conn, target.agent_id, row[0])
    freeze_resources(conn, target, row[0])
    return row[0]


@pytest.mark.parametrize("value", [None, {}, {"version": 2, "state": "admitted"}])
def test_unknown_or_bad_evidence_is_never_empty(value: object) -> None:
    with pytest.raises((ValueError, ResourceEvidenceError)):
        decode_resources(value)


def test_registration_and_attachment_require_exact_live_owner(db_conn: psycopg.Connection) -> None:
    target = _admitted(db_conn)
    entry = _entry()
    with db_conn.transaction():
        register_exec(db_conn, target, entry)
    for wrong in (
        RuntimeIncarnation(target.agent_id, uuid4(), target.owner),
        RuntimeIncarnation(target.agent_id, target.generation, uuid4()),
    ):
        with pytest.raises(ResourceEvidenceError), db_conn.transaction():
            register_exec(db_conn, wrong, _entry())
    attached = entry.model_copy(
        update={
            "owner_process": ResourceProcess(pid=100, birth=1.0),
            "root_process": ResourceProcess(pid=101, birth=2.0),
        }
    )
    with db_conn.transaction():
        attach_exec(db_conn, target, entry, attached)
    with pytest.raises(ResourceEvidenceError), db_conn.transaction():
        attach_exec(db_conn, target, entry, attached)


def test_force_freezes_registered_set_and_blocks_later_permit(db_conn: psycopg.Connection) -> None:
    target = _admitted(db_conn)
    entry = _entry()
    with db_conn.transaction():
        register_exec(db_conn, target, entry)
        force = _force(db_conn, target)
    with pytest.raises(ResourceEvidenceError), db_conn.transaction():
        register_exec(db_conn, target, _entry())
    with pytest.raises(ResourceEvidenceError), db_conn.transaction():
        attach_exec(
            db_conn,
            target,
            entry,
            entry.model_copy(
                update={
                    "owner_process": ResourceProcess(pid=100, birth=1.0),
                    "root_process": ResourceProcess(pid=101, birth=2.0),
                }
            ),
        )
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone()
    assert row is not None
    stored = decode_resources(row[0])
    assert isinstance(stored, IncarnationResources)
    assert stored.frozen_by == force and stored.requests == {str(entry.request): entry}
    db_conn.commit()


def test_force_lock_wins_over_concurrent_registration(db_conn: psycopg.Connection) -> None:
    target = _admitted(db_conn)
    started = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []
    with psycopg.connect(db_conn.info.dsn) as writer:
        path = db_conn.execute("SHOW search_path").fetchone()
        assert path is not None
        writer.execute("SELECT set_config('search_path',%s,false)", (path[0],))
        writer.commit()
        db_conn.commit()

        def register() -> None:
            try:
                with writer.transaction():
                    started.set()
                    register_exec(writer, target, _entry())
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        with db_conn.transaction():
            _force(db_conn, target)
            thread = threading.Thread(target=register)
            thread.start()
            assert started.wait(2)
            assert not finished.wait(0.1)
        thread.join(5)
        assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], ResourceEvidenceError)


@pytest.mark.parametrize("phase", ["register", "attach"])
def test_unchanged_locked_row_expiring_lease_never_permits_exec(
    db_conn: psycopg.Connection,
    phase: str,
) -> None:
    target = _admitted(db_conn)
    entry = _entry()
    with db_conn.transaction():
        if phase == "attach":
            register_exec(db_conn, target, entry)
        db_conn.execute(
            "UPDATE agents_meta SET lease_expires_at=clock_timestamp()+interval '0.3 seconds' WHERE id=%s",
            (target.agent_id,),
        )
    path = db_conn.execute("SHOW search_path").fetchone()
    assert path is not None
    db_conn.commit()
    errors: list[BaseException] = []
    started = threading.Event()
    with psycopg.connect(db_conn.info.dsn) as contender:
        contender.execute("SELECT set_config('search_path',%s,false)", (path[0],))
        contender.commit()

        def compete() -> None:
            try:
                with contender.transaction():
                    started.set()
                    if phase == "register":
                        register_exec(contender, target, entry)
                    else:
                        attach_exec(
                            contender,
                            target,
                            entry,
                            entry.model_copy(
                                update={
                                    "owner_process": ResourceProcess(pid=100, birth=1.0),
                                    "root_process": ResourceProcess(pid=101, birth=2.0),
                                }
                            ),
                        )
            except BaseException as exc:
                errors.append(exc)

        with db_conn.transaction():
            db_conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (target.agent_id,))
            thread = threading.Thread(target=compete)
            thread.start()
            assert started.wait(2)
            until = time.monotonic() + 5
            while time.monotonic() < until:
                row = db_conn.execute(
                    "SELECT lease_expires_at<=clock_timestamp() FROM agents_meta WHERE id=%s",
                    (target.agent_id,),
                ).fetchone()
                if row == (True,):
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("lease did not expire while row remained locked")
        thread.join(5)
        assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ResourceEvidenceError)
