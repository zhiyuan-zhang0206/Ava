"""A local host's proven exit permits takeover without weakening resource fences."""

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psutil
import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.hosted_ownership import admit_hosted_runtime
from shared.db import create_agent
from shared.incarnation_resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceEvidenceError,
    ResourceProcess,
    decode_resources,
)
from shared.managed_writer_publication import AdmissionDecision, CurrentAdmission
from shared.proc_tree import stable_create_time
from shared.runtime_admission import RuntimeAdmission
from shared.runtime_incarnation import RuntimeIncarnation


class _CurrentAdmission(RuntimeAdmission):
    async def decide_async(self, conn: psycopg.AsyncConnection) -> AdmissionDecision:
        del conn
        return CurrentAdmission(uuid4())


@pytest.fixture
def exited_host() -> ResourceProcess:
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        native = psutil.Process(process.pid)
        identity = ResourceProcess(pid=native.pid, birth=stable_create_time(native))
    finally:
        process.terminate()
        process.wait(timeout=5)
    return identity


def _seed(
    conn: psycopg.Connection, host: ResourceProcess, *, fence: str = "none"
) -> tuple[int, IncarnationResources]:
    agent_id = create_agent(conn)
    requests: dict[str, ExecAllocation] = {}
    if fence == "open_request":
        allocation = ExecAllocation(
            request=uuid4(),
            domain=uuid4(),
            request_digest="a" * 64,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        )
        requests[str(allocation.request)] = allocation
    evidence = IncarnationResources(
        generation=uuid4(),
        owner=uuid4(),
        host_process=None if fence == "unknown_host" else host,
        frozen_by=1 if fence == "frozen" else None,
        requests=requests,
    )
    conn.execute(
        "INSERT INTO agents_meta (id,status,machine,runtime_kind,runtime_generation,"
        "runtime_owner,incarnation_resources,lease_expires_at) "
        "VALUES (%s,'running',%s,'hosted',%s,%s,%s,clock_timestamp()+interval '10 minutes') "
        "ON CONFLICT(id) DO UPDATE SET status=EXCLUDED.status,machine=EXCLUDED.machine,"
        "runtime_kind=EXCLUDED.runtime_kind,runtime_generation=EXCLUDED.runtime_generation,"
        "runtime_owner=EXCLUDED.runtime_owner,incarnation_resources=EXCLUDED.incarnation_resources,"
        "lease_expires_at=EXCLUDED.lease_expires_at",
        (
            agent_id,
            "other-machine" if fence == "other_machine" else "host-test",
            evidence.generation,
            evidence.owner,
            Jsonb(evidence.model_dump(mode="json")),
        ),
    )
    conn.commit()
    return agent_id, evidence


@pytest.mark.parametrize("fence", ["other_machine", "open_request", "frozen", "unknown_host"])
async def test_dead_host_does_not_bypass_resource_or_machine_fences(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    exited_host: ResourceProcess,
    fence: str,
) -> None:
    agent_id, evidence = _seed(db_conn, exited_host, fence=fence)
    with pytest.raises(ResourceEvidenceError):
        await admit_hosted_runtime(
            aops_pool,
            agent_id,
            "host-test",
            uuid4(),
            expected_from="running",
            publication=_CurrentAdmission(None),
        )
    stored = db_conn.execute(
        "SELECT runtime_owner,incarnation_resources,lease_expires_at>clock_timestamp() "
        "FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone()
    assert stored == (evidence.owner, evidence.model_dump(mode="json"), True)


async def test_two_successors_of_dead_host_admit_only_one_owner(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    exited_host: ResourceProcess,
) -> None:
    agent_id, _ = _seed(db_conn, exited_host)
    results = await asyncio.gather(
        *(
            admit_hosted_runtime(
                aops_pool,
                agent_id,
                "host-test",
                uuid4(),
                expected_from="running",
                publication=_CurrentAdmission(None),
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    admitted = [value for value in results if isinstance(value, RuntimeIncarnation)]
    assert len(admitted) == 1
    assert sum(isinstance(value, ResourceEvidenceError) for value in results) == 1
    stored = db_conn.execute(
        "SELECT runtime_generation,runtime_owner,incarnation_resources FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone()
    assert stored is not None
    assert stored[:2] == (admitted[0].generation, admitted[0].owner)
    resources = decode_resources(stored[2])
    assert isinstance(resources, IncarnationResources)
    assert (resources.generation, resources.owner) == stored[:2]


async def test_reused_pid_identifies_old_host_exit_without_touching_replacement(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    native = psutil.Process()
    current = ResourceProcess(pid=native.pid, birth=stable_create_time(native))
    # A recycled pid cannot start within the 2.0s identity tolerance: the
    # predecessor's birth must lie beyond it to model a different process.
    prior = ResourceProcess(pid=current.pid, birth=current.birth - 60)
    agent_id, _ = _seed(db_conn, prior)
    admitted = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        "host-test",
        uuid4(),
        expected_from="running",
        publication=_CurrentAdmission(None),
    )
    assert admitted is not None
    assert native.is_running() and stable_create_time(native) == current.birth
    stored = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    assert stored is not None
    resources = decode_resources(stored[0])
    assert isinstance(resources, IncarnationResources)
    assert resources.host_process == current
