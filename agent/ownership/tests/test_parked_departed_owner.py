"""An idle agent whose owner host has departed is parked by the restart cohort.

A host that exits releases its idle agents' leases and leaves the owner and the
admitted-but-empty resource set on the row. Nothing wakes such an agent until a
message arrives, so after a host restart it stays in exactly that state. Only an
agent with work in flight must have the live original owner; the departed-owner
idle agent is parked and re-admitted by the ordinary admission path when it is
next woken.
"""

import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psutil
import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime
from base.agents.incarnation.resources import ExecAllocation, IncarnationResources, ResourceProcess
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.deploy.maintenance import admission, cohort, pause_owner
from base.deploy.tests.maintenance_factory import WHEN, maintenance_agent
from base.deploy.tests.maintenance_factory import isolate as isolate

_MINUTE_AGO = datetime.now(UTC) - timedelta(minutes=1)


@pytest.fixture
def departed_host() -> Iterator[ResourceProcess]:
    """The identity of a host process that has exited."""
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = ResourceProcess.capture(psutil.Process(child.pid))
    finally:
        child.kill()
        child.wait(timeout=5)
    yield identity


def _leave_with_departed_owner(
    conn: psycopg.Connection[Any],
    agent_id: int,
    departed: ResourceProcess,
    *,
    status: str = "idling",
    lease: datetime | None = None,
    pid: int | None = None,
    requests: dict[str, ExecAllocation] | None = None,
    owner: UUID | None = None,
) -> UUID:
    """The row a departed host leaves: owner kept, lease released, resources admitted."""
    old_owner = owner or uuid4()
    evidence = IncarnationResources(
        generation=uuid4(), owner=old_owner, host_process=departed, requests=requests or {}
    )
    conn.execute(
        "UPDATE agents_meta SET status=%s,runtime_kind='hosted',runtime_owner=%s,"
        "runtime_generation=%s,lease_expires_at=%s,pid=%s,incarnation_resources=%s "
        "WHERE id=%s",
        (
            status,
            old_owner,
            evidence.generation,
            lease,
            pid,
            Jsonb(evidence.model_dump(mode="json")),
            agent_id,
        ),
    )
    conn.commit()
    return old_owner


@pytest.mark.parametrize("lease", [None, _MINUTE_AGO], ids=["released", "expired"])
async def test_an_idle_agent_with_a_departed_owner_is_parked_and_readmitted(
    db_conn: psycopg.Connection[Any],
    aops_pool: AsyncConnectionPool[Any],
    database: Database,
    departed_host: ResourceProcess,
    machine_identity: MachineIdentity,
    lease: datetime | None,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    _leave_with_departed_owner(db_conn, agent, departed_host, lease=lease)

    pause_owner.begin_maintenance("owner", WHEN)
    hold = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=uuid4(),
        holder="owner",
        acquired_at=WHEN,
    )

    assert hold.parked == (agent,)
    assert hold.commands == {}
    cohort.verify_drained(db_conn, replace(hold, drained=()))

    # The hold is released; the next wake goes through the ordinary admission.
    pause_owner.change_maintenance("owner", WHEN, hold, hold, resumed=True)
    assert not admission.held()
    incarnation = await admit_hosted_runtime(
        aops_pool, agent, machine_identity.name(), uuid4(), expected_from="idling", db=database
    )
    assert incarnation is not None


async def test_parking_is_the_same_when_no_host_is_running(
    db_conn: psycopg.Connection[Any],
    departed_host: ResourceProcess,
    machine_identity: MachineIdentity,
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    _leave_with_departed_owner(db_conn, agent, departed_host)

    pause_owner.begin_maintenance("owner", WHEN)
    hold = cohort.prepare(
        db_conn,
        machine=machine_identity.name(),
        host_owner=None,
        holder="owner",
        acquired_at=WHEN,
        host_absent=True,
    )

    assert hold.parked == (agent,)


def _allocation() -> ExecAllocation:
    request = uuid4()
    return ExecAllocation(
        request=request,
        domain=uuid4(),
        request_digest="0" * 64,
        deadline=WHEN,
    )


@pytest.mark.parametrize(
    "work",
    [
        pytest.param({"status": "running"}, id="running turn"),
        pytest.param({"pid": 4242}, id="native process recorded"),
        pytest.param({"live_lease": True}, id="lease still live"),
        pytest.param({"managed_exec": True}, id="exec allocation outstanding"),
        pytest.param({"own_owner": True}, id="released by the live host itself"),
    ],
)
def test_work_in_flight_without_a_live_owner_is_still_refused(
    db_conn: psycopg.Connection[Any],
    departed_host: ResourceProcess,
    machine_identity: MachineIdentity,
    work: dict[str, Any],
) -> None:
    agent = maintenance_agent(db_conn, machine_name=machine_identity.name())
    live_host = uuid4()
    requests: dict[str, ExecAllocation] = {}
    if work.get("managed_exec"):
        allocation = _allocation()
        requests = {str(allocation.request): allocation}
    _leave_with_departed_owner(
        db_conn,
        agent,
        departed_host,
        status=work.get("status", "idling"),
        # Taken now, not at import: a shard collects minutes before it reaches this test.
        lease=datetime.now(UTC) + timedelta(minutes=1) if work.get("live_lease") else None,
        pid=work.get("pid"),
        requests=requests,
        owner=live_host if work.get("own_owner") else None,
    )

    pause_owner.begin_maintenance("owner", WHEN)
    with pytest.raises(RuntimeError, match="live original native owner"):
        cohort.prepare(
            db_conn,
            machine=machine_identity.name(),
            host_owner=live_host,
            holder="owner",
            acquired_at=WHEN,
        )
