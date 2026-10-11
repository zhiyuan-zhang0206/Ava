"""A lease or status change never substitutes for exact receiver/resource closure."""

from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.agents.compaction.commands import accept
from base.agents.compaction.execution import claim_attempt, pending, require_receiver
from base.agents.compaction.models import CompactHeldError
from base.agents.compaction.tests.helpers import source
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.db.transaction import async_write_transaction
from base.lm.catalog import ModelCatalog


@pytest.mark.parametrize("phase", ["source", "execution"])
@pytest.mark.parametrize("change", ["owner", "pointer", "frozen", "allocation"])
async def test_stale_or_unclosed_receiver_cannot_claim_or_apply_original(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    phase: str,
    change: str,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> None:
    incarnation, target, *_ = await source(
        db_conn,
        aops_pool,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
    )
    with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
        accept(pool, str(target.observation_id), target.source.agent_id, target)
    command = await pending(aops_pool, target.source.agent_id)
    assert command is not None
    if phase == "execution":
        command, fresh = await claim_attempt(aops_pool, command, incarnation, provider_key="gpt")
        assert fresh
    if change == "owner":
        db_conn.execute(
            "UPDATE agents_meta SET runtime_owner=%s WHERE id=%s", (uuid4(), target.source.agent_id)
        )
    elif change == "pointer":
        db_conn.execute(
            "UPDATE agents_meta SET native_work_id=%s WHERE id=%s",
            (uuid4(), target.source.agent_id),
        )
    else:
        row = db_conn.execute(
            "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.source.agent_id,)
        ).fetchone()
        assert row is not None
        resources = row[0]
        if change == "frozen":
            resources["frozen_by"] = 1
        else:
            request = str(uuid4())
            resources["requests"][request] = {
                "request": request,
                "domain": str(uuid4()),
                "request_digest": "0" * 64,
                "deadline": datetime.now(UTC).isoformat(),
                "owner_process": None,
                "root_process": None,
            }
        db_conn.execute(
            "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
            (Jsonb(resources), target.source.agent_id),
        )
    db_conn.commit()
    with pytest.raises(CompactHeldError):
        if phase == "source":
            await claim_attempt(aops_pool, command, incarnation, provider_key="gpt")
        else:
            async with async_write_transaction(aops_pool) as conn:
                await require_receiver(conn, command, incarnation)
    stored = await pending(aops_pool, target.source.agent_id)
    assert stored == command
