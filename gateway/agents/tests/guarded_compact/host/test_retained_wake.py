"""The existing scan retains accepted compaction beside ordinary work without a new lane."""

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from agent.tests.claim.test_inbound_ownership import agent_row, insert
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from gateway.agents.tests.guarded_compact.host.admission import admit
from gateway.tests.test_idempotency import client as client


async def test_actual_scan_keeps_pending_compact_and_later_chat_on_work_lane(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    other = agent_row(db_conn)
    insert(db_conn, other)
    quiet = agent_row(db_conn)
    work = set(await accepted.host.pending_inbound_wakes())
    assert work == {accepted.agent, other} and quiet not in work
    assert accepted.status(client)["outcome"] == "accepted"
    # No Redis signal is required to rediscover either accepted producer.
    again = await accepted.host.pending_inbound_wakes()
    assert set(again) == {accepted.agent, other}
