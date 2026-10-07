"""Keyed SDK writes derive an established actor and revalidate borrowed identity."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import psycopg
import pytest

from ava.sdk_surface import process_context
from ava_builtins.plugins.ava_fleet import task_registry
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import _seed_agent
from ava_builtins.plugins.ava_fleet.tests.test_task_registry import root_task_id as root_task_id
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity, ExternalLease
from base.db import Database
from tests.fixtures.pin_agent import pin_agent


def test_borrowed_identity_can_replay_native_operation_until_lease_ends(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    actor, owner = _seed_agent(db_conn), _seed_agent(db_conn)
    pin_agent(actor)
    original = task_registry.create("once", "work", parent=root_task_id, operation_key="native")
    active = Event()
    active.set()

    def validate() -> int:
        if not active.is_set():
            raise RuntimeError("lease expired")
        return actor

    pin_agent(owner, lease=ExternalLease(actor, validate, lambda: None))
    assert (
        task_registry.create("once", "work", parent=root_task_id, operation_key="native")
        == original
    )
    active.clear()
    with pytest.raises(RuntimeError, match="lease expired"):
        task_registry.create("once", "work", parent=root_task_id, operation_key="native")
    pin_agent(actor)
    assert task_registry.get(original.id) == original
    assert db_conn.execute("SELECT count(*) FROM task_creation_receipts").fetchone() == (1,)


def test_lease_expiring_during_operation_lock_wait_cannot_write(
    db_conn: psycopg.Connection, root_task_id: int
) -> None:
    actor, owner = _seed_agent(db_conn), _seed_agent(db_conn)
    pin_agent(actor)
    active, validated = Event(), Event()
    active.set()

    def validate() -> int:
        if not active.is_set():
            raise RuntimeError("lease expired after lock wait")
        validated.set()
        return actor

    def append() -> None:
        clients = ClientSet(database=Database.from_settings)
        try:
            identity = AgentIdentity(
                owner, False, lease=ExternalLease(actor, validate, lambda: None)
            )
            with process_context.scoped(AvaContext(identity=identity, clients=clients)):
                task_registry.create("stale", "work", parent=root_task_id, operation_key="wait")
        finally:
            clients.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with db_conn.transaction():
            db_conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"task-create:{actor}:wait",),
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
    assert db_conn.execute("SELECT count(*) FROM agent_tasks WHERE title='stale'").fetchone() == (
        0,
    )
    assert db_conn.execute("SELECT count(*) FROM task_creation_receipts").fetchone() == (0,)
