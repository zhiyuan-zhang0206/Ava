from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.tests.claim.test_inbound_ownership import insert
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import AddBindings
from gateway.tests.lifecycle.guarded_compact.host.admission import admit
from gateway.tests.lifecycle.guarded_compact.host.faults import install
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host import host as host_module
from services.agent_runner.agent_host.tests.guarded_compact_support import SummaryModel


@pytest.mark.parametrize("failure", ["operational", "pool", "provider", "short"])
async def test_generation_unknown_or_short_never_repeats_and_next_chat_runs(
    redis_url: str,
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
        insert(db_conn, accepted.agent)
        await accepted.host.run_turn(accepted.agent)
        assert calls == [failure] and len(accepted.ordinary) == 1
        return
    await accepted.host.run_turn(accepted.agent)
    status = accepted.status(client)
    assert status["outcome"] == "rejected" and status["continuation_released"]
    assert calls == [failure]
    insert(db_conn, accepted.agent)
    await accepted.host.run_turn(accepted.agent)
    assert calls == [failure] and len(accepted.ordinary) == 2


@pytest.mark.parametrize("retained", ["prepared", "applying", "rejected"])
async def test_later_wake_holds_a_retained_result_without_resuming_or_crashing(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    retained: str,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    model = SummaryModel(responses=["Original source summary. " * 100])
    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog, {"gpt-": replace(binding, build_single_attempt=lambda _: model)}
    )
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    install(monkeypatch, "after_result")
    with pytest.raises(psycopg.OperationalError):
        await accepted.host.run_turn(accepted.agent)
    db_conn.execute(
        "UPDATE native_compact_commands SET outcome=%s WHERE id=%s",
        (retained, accepted.acceptance["command_id"]),
    )
    db_conn.commit()
    before: dict[str, Any] = accepted.status(client)
    assert before["result_available"] and not before["continuation_released"]
    recoveries: list[int] = []
    recover = host_module.recover_native_cancel

    async def spy(*args: Any, **kwargs: Any) -> bool:
        recoveries.append(1)
        return await recover(*args, **kwargs)

    monkeypatch.setattr(host_module, "recover_native_cancel", spy)

    insert(db_conn, accepted.agent)
    await accepted.host.run_turn(accepted.agent)

    assert model.calls == 1 and len(accepted.ordinary) == 1
    assert accepted.status(client) == before
    assert recoveries == [1]
