"""Keyed SDK writes derive an established actor and revalidate borrowed identity."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import psycopg
import pytest

from ava_builtins.plugins.ava_fleet import task_registry
from ava_builtins.plugins.ava_fleet.tests.task_registry.test_update_receipts import _setup
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import root_task_id as root_task_id
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from tests.fixtures.pin_agent import pin_agent


@pytest.mark.parametrize("key", ["", "x" * 129, 23])
def test_invalid_key_fails_without_receipt(
    db_conn: psycopg.Connection, root_task_id: int, key: object
) -> None:
    _actor, _owner, tid = _setup(db_conn, root_task_id)
    with pytest.raises((ValueError, TypeError), match="idempotency key"):
        task_registry.log(tid, "invalid", operation_key=key)  # type: ignore[arg-type]
    assert task_registry.get(tid).results is None
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (0,)


def test_mutation_requires_key_and_established_actor(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    _actor, _owner, tid = _setup(db_conn, root_task_id)
    pin_agent(None)
    with pytest.raises(RuntimeError, match="no established agent identity"):
        task_registry.log(tid, "forbidden", operation_key="system")
    missing: dict[str, str] = {}
    with pytest.raises(TypeError, match="operation_key"):
        task_registry.log(tid, "keyless", **missing)
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (0,)
    result = task_registry.get(tid).results
    assert result is None


def test_borrowed_identity_can_replay_native_operation_until_lease_ends(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    actor, owner, tid = _setup(db_conn, root_task_id)
    task_registry.log(tid, "once", operation_key="native")
    active = Event()
    active.set()

    def validate() -> int:
        if not active.is_set():
            raise RuntimeError("lease expired")
        return actor

    pin_agent(owner, lease=ExternalLease(actor, validate, lambda: None))
    assert task_registry.log(tid, "once", operation_key="native") is None
    active.clear()
    with pytest.raises(RuntimeError, match="lease expired"):
        task_registry.log(tid, "once", operation_key="native")
    pin_agent(actor)
    result = task_registry.get(tid).results
    assert result is not None and result.count("once") == 1
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (1,)


def test_lease_expiring_during_operation_lock_wait_cannot_write(
    db_conn: psycopg.Connection, root_task_id: int, *, database_gate: ProcessDbGate
) -> None:
    actor, owner, tid = _setup(db_conn, root_task_id)
    active, validated = Event(), Event()
    active.set()

    def validate() -> int:
        if not active.is_set():
            raise RuntimeError("lease expired after lock wait")
        validated.set()
        return actor

    def append() -> None:
        clients = ClientSet(database=lambda: Database.from_settings(gate=database_gate))
        try:
            identity = AgentIdentity(
                owner, False, lease=ExternalLease(actor, validate, lambda: None)
            )
            context = AvaContext(identity=identity, clients=clients)
            task_registry._update(context, tid, note="stale", operation_key="wait")
        finally:
            clients.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with db_conn.transaction():
            db_conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"task-update:{actor}:{tid}:wait",),
            )
            future = executor.submit(append)
            assert validated.wait(5)
            deadline = time.monotonic() + 5
            blocked = False
            while time.monotonic() < deadline:
                if future.done():
                    future.result()
                db_conn.execute("SELECT pg_stat_clear_snapshot()")
                blocked = db_conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                    "WHERE pg_backend_pid()=ANY(pg_blocking_pids(pid)))"
                ).fetchone() == (True,)
                if blocked:
                    break
                time.sleep(0.01)
            assert blocked, "SDK operation must actually wait on the held receipt key"
            active.clear()
        with pytest.raises(RuntimeError, match="expired after lock wait"):
            future.result(timeout=5)
    assert task_registry.get(tid).results is None
    assert db_conn.execute("SELECT count(*) FROM task_update_receipts").fetchone() == (0,)
