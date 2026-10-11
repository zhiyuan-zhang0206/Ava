"""Losing the live continuation after business completion cannot skip native closure."""

from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from agent.tests.claim.test_inbound_ownership import _insert
from base.agents.compaction.models import CompactHeldError
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.invocation.compact import execute as compact_execute
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel
from services.agent_runner.agent_host.tests.guarded_compact.test_late_cancel import accept_cancel
from tests.fixtures.model_catalog import AddBindings


@pytest.mark.parametrize("short", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_outer_owned_resource_closure_finishes_business_terminal_receipt(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    short: bool,
    cancel: bool,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    model = SummaryModel(responses=["short" if short else "Original source summary. " * 100])
    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog, {"gpt-": replace(binding, build_single_attempt=lambda _: model)}
    )
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    cancelled: list[dict[str, Any]] = []

    async def lose_continuation(*args: Any, **kwargs: Any) -> bool:
        if cancel:
            cancelled.append(accept_cancel(client, accepted))
        raise CompactHeldError("process continuation lost before native closure")

    with monkeypatch.context() as patch:
        patch.setattr(compact_execute, "close_terminal", lose_continuation)
        with pytest.raises(CompactHeldError, match="continuation lost"):
            await accepted.host.run_turn(accepted.agent)
    before = accepted.status(client)
    assert before["outcome"] == ("rejected" if short else "applied")
    assert before["continuation_released"] and model.calls == 1
    assert db_conn.execute(
        "SELECT phase,ended_at IS NOT NULL FROM native_graph_work WHERE id=%s",
        (before["execution"]["work_id"],),
    ).fetchone() == ("settled", True)
    assert (
        client.get(accepted.path + "/compact-target", headers=accepted.headers).status_code == 200
    )
    _insert(db_conn, accepted.agent)
    await accepted.host.run_turn(accepted.agent)
    after = accepted.status(client)
    assert after["continuation_released"] and after["checkpoint_id"] == before["checkpoint_id"]
    assert after["attempt_id"] == before["attempt_id"] and model.calls == 1
    assert db_conn.execute(
        "SELECT phase,ended_at IS NOT NULL FROM native_graph_work WHERE id=%s",
        (before["execution"]["work_id"],),
    ).fetchone() == ("settled", True)
    assert (
        len(accepted.ordinary) == 2 and str(accepted.ordinary[-1]) != before["execution"]["work_id"]
    )
    if cancel:
        assert db_conn.execute(
            "SELECT outcome FROM native_cancel_commands WHERE id=%s", (cancelled[0]["command_id"],)
        ).fetchone() == ("applied",)
    assert (
        client.get(accepted.path + "/compact-target", headers=accepted.headers).status_code == 200
    )
