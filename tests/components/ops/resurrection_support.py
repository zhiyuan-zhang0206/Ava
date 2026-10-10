"""Shared SQL row and lifecycle receipt setup for resurrection tests."""

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.cluster.machine import machine_name
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.db import PG_KEEPALIVE_KWARGS, Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents.spawn import create_agent_row
from ops.lifecycle import termination


def terminated(
    db: psycopg.Connection,
    resources: object,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    aid, _, _, _ = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=machine_name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    db.execute(
        "UPDATE agents_meta SET status='terminated',termination_source='user',"
        "incarnation_resources=%s WHERE id=%s",
        (None if resources is None else Jsonb(resources), aid),
    )
    db.commit()
    return aid


def status(db: psycopg.Connection, aid: int) -> tuple[str, str | None]:
    row = db.execute("SELECT status,runtime_kind FROM agents_meta WHERE id=%s", (aid,)).fetchone()
    db.commit()
    assert row is not None
    return row[0], row[1]


def force(aid: int) -> int:
    with ConnectionPool[psycopg.Connection](
        settings.data_plane.db_url, min_size=1, max_size=1, kwargs=PG_KEEPALIVE_KWARGS
    ) as pool:
        _, _, _, force, _cutoff = termination._force_terminate_transaction(aid, pool, source="user")
    return force


def unowned_receipt(db: psycopg.Connection, command: int) -> bool:
    row = db.execute(
        "SELECT payload->'unowned_termination' FROM inbound_messages WHERE id=%s", (command,)
    ).fetchone()
    db.commit()
    return row == (True,)


def legacy_row(
    db: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
) -> int:
    """A row no birth epoch vouches for: only a later lifecycle release can."""
    aid, _, _, _ = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(),
        spawner="user",
        machine=machine_name(),
        authority=config_authority,
        catalog=model_catalog,
    )
    db.execute(
        "UPDATE agents_meta SET last_resurrect_inbound_id=NULL,incarnation_resources=NULL "
        "WHERE id=%s",
        (aid,),
    )
    db.commit()
    return aid
