"""Unknown provider results never regenerate; unusable results close native execution."""

from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.tests.test_inbound_ownership import _insert
from base.lm.plugin_providers import model_catalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.invocation.compact.checkpoint import cold_reader
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

    binding = model_catalog().bindings["gpt-"]
    add_bindings(
        {"gpt-": replace(binding, build_single_attempt=lambda _: FailingModel(responses=["short"]))}
    )
    accepted = await admit(db_conn, aops_pool, client, monkeypatch)
    await accepted.host.run_turn(accepted.agent)
    status = accepted.status(client)
    assert status["outcome"] == ("rejected" if failure == "short" else "uncertain")
    assert status["checkpoint_id"] is None and calls == [failure]
    if failure != "short":
        assert status["recovery_checkpoint_id"] and not status["result_available"]
    work = db_conn.execute(
        "SELECT phase,ended_at FROM native_graph_work WHERE id=%s",
        (status["execution"]["work_id"],),
    ).fetchone()
    assert work is not None and work[0] == "settled" and work[1] is not None
    persisted = await cold_reader(accepted.saver).aget_tuple(accepted.config)
    assert persisted is not None
    assert any(m.id == "source-history" for m in persisted.checkpoint["channel_values"]["messages"])
    _insert(db_conn, accepted.agent)
    await accepted.host.run_turn(accepted.agent)
    assert calls == [failure] and len(accepted.ordinary) == 2
    assert str(accepted.ordinary[-1]) != status["execution"]["work_id"]
