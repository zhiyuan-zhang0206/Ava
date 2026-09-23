"""Actual hosted admission preserves pending work during publication maintenance."""

from datetime import datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.hosted_ownership import admit_hosted_runtime
from shared.db import create_agent
from shared.managed_writer_publication import (
    AdmissionDecision,
    CurrentAdmission,
    WriterPublication,
)
from shared.runtime_admission import (
    PublicationAdmissionDeferredError,
    RuntimeAdmission,
    require_activation,
)
from tests.shared.test_managed_writer_publication import publication_db as publication_db
from tests.shared.test_managed_writer_publication import seed_current


@pytest.mark.usefixtures("publication_db")
@pytest.mark.parametrize("missing", ["digest", "challenge", "both", "neither"])
def test_current_requires_both_actual_activation_fields(
    db_conn: psycopg.Connection, missing: str
) -> None:
    current = seed_current(db_conn)
    current = current.model_copy(
        update={
            "activation_digest": None if missing in {"digest", "both"} else "a" * 64,
            "activation_challenge": None if missing in {"challenge", "both"} else uuid4(),
        }
    )
    db_conn.execute(
        "UPDATE deployment_state SET managed_writer_evidence=%s",
        (Jsonb(WriterPublication(current=current).model_dump(mode="json")),),
    )
    decision = CurrentAdmission(current.publication_id)
    with db_conn.transaction():
        if missing == "neither":
            require_activation(db_conn, decision)
        else:
            with pytest.raises(PublicationAdmissionDeferredError, match="verified activation"):
                require_activation(db_conn, decision)


def _admission_agent(conn: psycopg.Connection) -> int:
    agent_id = create_agent(conn)
    conn.execute(
        "INSERT INTO agents_meta (id,status,machine) VALUES (%s,'idling','host-test') "
        "ON CONFLICT (id) DO UPDATE SET status='idling',machine='host-test'",
        (agent_id,),
    )
    conn.commit()
    return agent_id


def _admission_observation(
    conn: psycopg.Connection, agent_id: int
) -> tuple[str | None, datetime | None]:
    row = conn.execute(
        "SELECT last_admission_outcome,last_admission_at FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone()
    assert row is not None
    return row[0], row[1]


async def test_deferred_admission_is_recorded_then_success_supersedes_it(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, owner = _admission_agent(db_conn), uuid4()

    class _Deferred(RuntimeAdmission):
        async def decide_async(self, conn: psycopg.AsyncConnection) -> AdmissionDecision:
            del conn
            raise PublicationAdmissionDeferredError("deferred for the test")

    refused = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        "host-test",
        owner,
        expected_from="idling",
        publication=_Deferred(None),
    )
    assert refused is None
    refused_code, refused_at = _admission_observation(db_conn, agent_id)
    assert refused_code == "publication_deferred" and refused_at is not None
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (agent_id,)
    ).fetchone() == ("idling",)

    admitted = await admit_hosted_runtime(
        aops_pool, agent_id, "host-test", owner, expected_from="idling"
    )
    assert admitted is not None
    code, admitted_at = _admission_observation(db_conn, agent_id)
    assert code == "admitted" and admitted_at is not None and admitted_at >= refused_at


async def test_guard_refusal_records_only_a_coarse_code(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
) -> None:
    agent_id, owner = _admission_agent(db_conn), uuid4()
    db_conn.execute("UPDATE agents_meta SET pid=12345 WHERE id=%s", (agent_id,))
    db_conn.commit()

    assert (
        await admit_hosted_runtime(aops_pool, agent_id, "host-test", owner, expected_from="idling")
        is None
    )
    code, observed_at = _admission_observation(db_conn, agent_id)
    assert code == "admission_guard_refused" and observed_at is not None
