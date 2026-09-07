"""Same-host resurrection still requires durable predecessor resource closure."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.hosted_ownership import admit_hosted_runtime, apply_hosted_lifecycle
from ops.agent_spawn import create_agent_row
from ops.agent_wake import resurrect_agent
from shared.db import insert_inbound_message
from shared.incarnation_resources import (
    ExecAllocation,
    IncarnationResources,
    ResourceBirth,
    ResourceEvidenceError,
    ResourceProcess,
    decode_resources,
)
from shared.machine import machine_name
from shared.turn_identity import bind_turn_identity


async def _resurrected(
    db: psycopg.Connection, pool: AsyncConnectionPool
) -> tuple[int, int, UUID, IncarnationResources]:
    aid, _ = create_agent_row(spawner="user", machine=machine_name())
    db.execute(
        "UPDATE agents_meta SET status='idling',incarnation_resources=%s WHERE id=%s",
        (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), aid),
    )
    db.commit()
    owner = uuid4()
    old = await admit_hosted_runtime(pool, aid, machine_name(), owner, expected_from="idling")
    assert old is not None
    command = insert_inbound_message(db, aid, "", "self", kind="terminate")
    with bind_turn_identity(aid, incarnation=old):
        assert [item.id for item in await claim_inbound_batch(pool, aid)] == [command]
        assert await apply_hosted_lifecycle(pool, old) == "terminate"
    resurrect_agent(aid, resurrected_by="user")
    row = db.execute("SELECT incarnation_resources FROM agents_meta WHERE id=%s", (aid,)).fetchone()
    assert row is not None
    resources = decode_resources(row[0])
    assert isinstance(resources, IncarnationResources)
    return aid, command, owner, resources


@pytest.mark.parametrize("missing", ["applied", "observed", "resource_closure"])
async def test_same_host_cannot_skip_missing_predecessor_evidence(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, missing: str
) -> None:
    aid, command, owner, resources = await _resurrected(db_conn, aops_pool)
    if missing == "resource_closure":
        request = uuid4()
        allocation = ExecAllocation(
            request=request,
            domain=uuid4(),
            request_digest="a" * 64,
            deadline=datetime.now(UTC) + timedelta(minutes=1),
        )
        resources = resources.model_copy(update={"requests": {str(request): allocation}})
        db_conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(resources.model_dump(mode="json")), aid),
        )
    elif missing == "applied":
        db_conn.execute(
            "UPDATE inbound_messages SET status='claimed',applied_at=NULL,observed_at=NULL "
            "WHERE id=%s",
            (command,),
        )
    else:
        db_conn.execute(
            "UPDATE inbound_messages SET status='claimed',observed_at=NULL WHERE id=%s",
            (command,),
        )
    db_conn.commit()

    with pytest.raises(ResourceEvidenceError, match="predecessor resource/lifecycle closure"):
        await admit_hosted_runtime(aops_pool, aid, machine_name(), owner, expected_from="idling")

    assert db_conn.execute(
        "SELECT status,runtime_generation,runtime_owner,incarnation_resources "
        "FROM agents_meta WHERE id=%s",
        (aid,),
    ).fetchone() == ("idling", None, None, resources.model_dump(mode="json"))


async def test_same_pid_different_birth_requires_exact_predecessor_exit(
    db_conn: psycopg.Connection, aops_pool: AsyncConnectionPool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shared import exec_owner_recovery

    aid, _command, owner, resources = await _resurrected(db_conn, aops_pool)
    assert resources.host_process is not None
    prior_process = resources.host_process.model_copy(
        update={"birth": resources.host_process.birth + 1}
    )
    resources = resources.model_copy(update={"host_process": prior_process})
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
        (Jsonb(resources.model_dump(mode="json")), aid),
    )
    db_conn.commit()
    checked: list[ResourceProcess] = []

    def unresolved(identity: ResourceProcess) -> bool:
        checked.append(identity)
        return False

    monkeypatch.setattr(exec_owner_recovery, "process_ended", unresolved)

    assert (
        await admit_hosted_runtime(aops_pool, aid, machine_name(), owner, expected_from="idling")
        is None
    )
    assert checked == [prior_process]
