from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import AddBindings
from gateway.tests.lifecycle.guarded_compact.host.admission import admit
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.invocation.compact import execute as compact_execute
from services.agent_runner.agent_host.tests.guarded_compact_support import SummaryModel


async def test_commit_response_lost_before_provider_call_is_unknown_without_retry(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    models: list[SummaryModel] = []

    def build(_: Any) -> SummaryModel:
        model = SummaryModel(responses=["Must never be generated. " * 100])
        models.append(model)
        return model

    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog, {"gpt-": replace(binding, build_single_attempt=build)}
    )
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    original = compact_execute.claim_attempt
    claimed: list[str] = []

    async def claim(*args: Any, **kwargs: Any) -> Any:
        command, fresh = await original(*args, **kwargs)
        if not claimed:
            assert fresh and command.attempt_id is not None
            claimed.append(str(command.attempt_id))
            raise psycopg.OperationalError("original attempt commit response lost")
        assert not fresh and str(command.attempt_id) == claimed[0]
        return command, fresh

    monkeypatch.setattr(compact_execute, "claim_attempt", claim)
    with pytest.raises(psycopg.OperationalError):
        await accepted.host.run_turn(accepted.agent)
    status = accepted.status(client)
    assert status["outcome"] == "accepted" and status["attempt_id"] == claimed[0]
    assert status["recovery_checkpoint_id"] is None and status["checkpoint_id"] is None
    assert not status["result_available"] and models and all(model.calls == 0 for model in models)

    await accepted.host.run_turn(accepted.agent)
    assert all(model.calls == 0 for model in models)
    assert accepted.status(client) == status
