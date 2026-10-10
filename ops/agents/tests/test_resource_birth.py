"""Actual birth owns resource origin; admission and replay cannot invent it."""

from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime
from base.agents.incarnation.resources import IncarnationResources, ResourceBirth, decode_resources
from base.cluster.machine import machine_name
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.lm.plugin_providers import build_model_catalog
from ops.agents.birth_transaction import insert_agent_birth
from ops.agents.creation_identity import creation_request_hash


def test_resource_birth_rolls_back_with_its_agent(
    db_conn: psycopg.Connection, *, config_authority: ConfigAuthority
) -> None:
    agent_id: int | None = None
    with pytest.raises(RuntimeError, match="refused"), db_conn.transaction():
        with db_conn.cursor() as cur:
            born = insert_agent_birth(
                cur,
                machine=machine_name(),
                catalog=build_model_catalog(),
                authority=config_authority,
            )
            agent_id = born.agent_id
        row = db_conn.execute(
            "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (born.agent_id,)
        ).fetchone()
        assert row is not None and isinstance(decode_resources(row[0]), ResourceBirth)
        raise RuntimeError("refused")
    assert agent_id is not None
    assert db_conn.execute("SELECT 1 FROM agents WHERE id=%s", (agent_id,)).fetchone() is None


async def test_real_first_admission_consumes_birth_and_replay_preserves_resources(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
) -> None:
    machine = machine_name()
    digest = creation_request_hash({"machine": machine})
    with db_conn.transaction(), db_conn.cursor() as cur:
        born = insert_agent_birth(
            cur,
            machine=machine,
            creation_key="resource-origin",
            creation_request_hash=digest,
            catalog=build_model_catalog(),
            authority=config_authority,
        )
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (born.agent_id,)
    ).fetchone()
    assert row is not None and isinstance(decode_resources(row[0]), ResourceBirth)
    db_conn.commit()
    admitted = await admit_hosted_runtime(
        aops_pool,
        born.agent_id,
        machine,
        uuid4(),
        expected_from="idling",
        db=Database.from_settings(),
    )
    assert admitted is not None
    before = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (born.agent_id,)
    ).fetchone()
    assert before is not None
    resources = decode_resources(before[0])
    assert isinstance(resources, IncarnationResources) and resources.host_process is not None
    assert resources.owner == admitted.owner and resources.generation == admitted.generation
    db_conn.commit()
    with db_conn.transaction(), db_conn.cursor() as cur:
        replay = insert_agent_birth(
            cur,
            machine=machine,
            creation_key="resource-origin",
            creation_request_hash=digest,
            catalog=build_model_catalog(),
            authority=config_authority,
        )
    assert replay.agent_id == born.agent_id
    assert (
        db_conn.execute(
            "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (born.agent_id,)
        ).fetchone()
        == before
    )
