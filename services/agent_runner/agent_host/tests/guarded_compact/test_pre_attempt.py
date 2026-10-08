"""Rejected construction without an attempt cannot starve later ordinary input."""

from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from agent.tests.claim.test_inbound_ownership import _insert
from base.config import settings
from base.lm.plugin_providers import model_catalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit
from tests.fixtures.model_catalog import AddBindings


@pytest.mark.parametrize("unsupported", ["binding", "override"])
async def test_pre_attempt_construction_rejected_without_provider_or_starvation(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    unsupported: str,
) -> None:
    accepted = await admit(db_conn, aops_pool, client, monkeypatch)
    if unsupported == "binding":
        binding = model_catalog().bindings["gpt-"]
        add_bindings({"gpt-": replace(binding, build_single_attempt=None)})
    else:
        monkeypatch.setattr(settings.lm, "llm_override", "gpt-6.1-sol")
    await accepted.host.run_turn(accepted.agent)
    status = accepted.status(client)
    assert status["outcome"] == "rejected" and status["reason"] == "single_attempt_unavailable"
    assert status["continuation_released"] and status["attempt_id"] is None
    assert status["execution"] is None and not status["result_available"]
    assert len(accepted.ordinary) == 1
    _insert(db_conn, accepted.agent)
    await accepted.host.run_turn(accepted.agent)
    assert len(accepted.ordinary) == 2
