from uuid import uuid4

import psycopg
import pytest

from ava.tests.fixtures.pin_agent import pin_agent
from ava_builtins.plugins.ava_fleet import task_registry
from ava_builtins.plugins.ava_fleet.tests.task_registry.seeds import root_task_id as root_task_id
from ava_builtins.plugins.ava_fleet.tests.task_registry.seeds import seed_agent
from base.api_contracts.idempotency import keyed_headers
from base.cluster.machine import MachineIdentity
from gateway.routers import tasks
from tests.fixtures.gateway_config import gateway_test_client


def test_gateway_enqueue_failure_rolls_back_assignment(
    redis_url: str,
    db_conn: psycopg.Connection,
    root_task_id: int,
    monkeypatch: pytest.MonkeyPatch,
    machine_identity: MachineIdentity,
) -> None:
    actor = seed_agent(db_conn)
    old_owner = seed_agent(db_conn)
    new_owner = seed_agent(db_conn)
    pin_agent(actor)
    task = task_registry.create(
        "atomic", "work", parent=root_task_id, owner=old_owner, operation_key=str(uuid4())
    )
    real = tasks.enqueue_task_notifications

    def unavailable(*args: object) -> None:
        raise psycopg.OperationalError("queue unavailable")

    with gateway_test_client(raise_server_exceptions=False, machine=machine_identity) as client:
        monkeypatch.setattr(tasks, "enqueue_task_notifications", unavailable)
        assert (
            client.patch(
                f"/api/tasks/{task.id}",
                json={"owner": new_owner},
                headers=keyed_headers(str(uuid4())),
            ).status_code
            == 500
        )
        with db_conn.cursor() as cur:
            cur.execute("SELECT owner FROM agent_tasks WHERE id=%s", (task.id,))
            assert cur.fetchone() == (old_owner,)
            cur.execute("SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (new_owner,))
            assert cur.fetchone() == (0,)
        monkeypatch.setattr(tasks, "enqueue_task_notifications", real)
        assert (
            client.patch(
                f"/api/tasks/{task.id}",
                json={"owner": new_owner},
                headers=keyed_headers(str(uuid4())),
            ).status_code
            == 200
        )
