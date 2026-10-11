"""Unknown provider results never regenerate; unusable results close native execution."""

from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.tests.claim.test_inbound_ownership import _insert
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel
from tests.fixtures.model_catalog import AddBindings


@pytest.mark.parametrize("failure", ["operational", "pool", "provider", "short"])
async def test_generation_unknown_or_short_never_repeats_and_next_chat_runs(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    failure: str,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    calls: list[str] = []

    class FailingModel(SummaryModel):
        async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
            calls.append(failure)
            if failure == "operational":
                raise psycopg.OperationalError("unknown provider response")
            if failure == "pool":
                raise PoolTimeout("unknown provider response")
            if failure == "provider":
                raise RuntimeError("unknown provider response")
            return await super().ainvoke(*args, **kwargs)

    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog,
        {
            "gpt-": replace(
                binding, build_single_attempt=lambda _: FailingModel(responses=["short"])
            )
        },
    )
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    if failure != "short":
        with pytest.raises(Exception, match="unknown provider response"):
            await accepted.host.run_turn(accepted.agent)
        status = accepted.status(client)
        assert status["outcome"] == "uncertain"
        assert not status["continuation_released"] and status["recovery_checkpoint_id"] is None
        assert calls == [failure]
        _insert(db_conn, accepted.agent)
        await accepted.host.run_turn(accepted.agent)
        assert calls == [failure] and len(accepted.ordinary) == 1
        return
    await accepted.host.run_turn(accepted.agent)
    status = accepted.status(client)
    assert status["outcome"] == "rejected" and status["continuation_released"]
    assert calls == [failure]
    _insert(db_conn, accepted.agent)
    await accepted.host.run_turn(accepted.agent)
    assert calls == [failure] and len(accepted.ordinary) == 2
