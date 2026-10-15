from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog

THRESHOLD_S = 300.0


@pytest.fixture
def pool(db_url: str):
    p = ConnectionPool(settings.data_plane.db_url, min_size=1, max_size=2, open=True)
    try:
        yield p
    finally:
        p.close()


def make_idle(
    db: psycopg.Connection,
    *,
    status_changed_s_ago: float,
    last_active_s_ago: float | None = None,
    paused_until_s_ahead: float | None = None,
    status: str = "idling",
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentity,
) -> int:
    if last_active_s_ago is None:
        last_active_s_ago = status_changed_s_ago
    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=local_machine,
    )
    with db.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET status = %s, "
            "lease_expires_at = now() + make_interval(secs => 600) WHERE id = %s",
            (status, aid),
        )
        cur.execute(
            "UPDATE agents_meta SET status_changed_at = now() - make_interval(secs => %s), "
            "       last_active_at = now() - make_interval(secs => %s) "
            "WHERE id = %s",
            (status_changed_s_ago, last_active_s_ago, aid),
        )
        if paused_until_s_ahead is not None:
            cur.execute(
                "UPDATE agents_meta SET heartbeat_paused_until = now() + make_interval(secs => %s) "
                "WHERE id = %s",
                (paused_until_s_ahead, aid),
            )
    db.commit()
    return aid


def lease(
    db: psycopg.Connection,
    agent_id: int,
    *,
    status: str = "active",
    delta_version: int = 0,
    applied_version: int = 0,
    automatic: bool = False,
    handoff_applied: bool = False,
    local_machine: MachineIdentity,
) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,"
            "ttl_seconds,expires_at,session_id,plugin_delta,delta_version,"
            "applied_version,automatic,handoff_applied_at) "
            "VALUES(%s,%s,'external_agent:codex:test',%s,%s,300,"
            "clock_timestamp()+interval '5 minutes',0,%s,%s,%s,%s,"
            "CASE WHEN %s THEN clock_timestamp() ELSE NULL END)",
            (
                uuid4(),
                agent_id,
                local_machine.name(),
                status,
                Jsonb([{} for _ in range(delta_version)]),
                delta_version,
                applied_version,
                automatic,
                handoff_applied,
            ),
        )
    db.commit()
