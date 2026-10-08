"""The original provider usage survives result loss and anchors the exact source segment."""

from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from psycopg_pool import AsyncConnectionPool

from base.agents.history.closing_request import ClosingRequest
from base.lm.plugin_providers import model_catalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.guarded_compact.admission import admit
from services.agent_runner.agent_host.tests.guarded_compact.faults import install
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel
from tests.fixtures.model_catalog import AddBindings


class UsageSummary(SummaryModel):
    def _generate(self, *args: Any, **kwargs: Any) -> Any:
        response = super()._generate(*args, **kwargs)
        message = response.generations[0].message
        assert isinstance(message, AIMessage)
        message.usage_metadata = {"input_tokens": 4000, "output_tokens": 500, "total_tokens": 4500}
        message.response_metadata = {"model_name": "original-provider-model"}
        return response


@pytest.mark.parametrize("fault", ["after_result", "after_ack"])
async def test_saved_original_closing_usage_stamps_source_before_replacement(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    fault: str,
) -> None:
    model = UsageSummary(responses=["Original anchored summary. " * 100])
    binding = model_catalog().bindings["gpt-"]
    add_bindings({"gpt-": replace(binding, build_single_attempt=lambda _: model)})
    accepted = await admit(db_conn, aops_pool, client, monkeypatch)
    install(monkeypatch, fault)
    await accepted.host.run_turn(accepted.agent)
    status = accepted.status(client)
    assert status["outcome"] == "applied" and status["continuation_released"]
    original = db_conn.execute(
        "SELECT result->'closing' FROM native_compact_commands WHERE id=%s",
        (accepted.acceptance["command_id"],),
    ).fetchone()
    assert original is not None
    closing = ClosingRequest.from_metadata(original[0])
    assert closing is not None and closing.input_tokens == 4000
    assert closing.model == "original-provider-model" and closing.extra_tokens > 0
    source = db_conn.execute(
        "SELECT metadata FROM checkpoints WHERE thread_id=%s AND checkpoint_ns='' "
        "AND checkpoint_id=%s",
        (str(accepted.agent), accepted.target["checkpoint_id"]),
    ).fetchone()
    assert source is not None and source[0]["compact_boundary"] is True
    assert source[0]["compact_anchor"] == original[0]
    applied = db_conn.execute(
        "SELECT metadata FROM checkpoints WHERE thread_id=%s AND checkpoint_ns='' "
        "AND checkpoint_id=%s",
        (str(accepted.agent), status["checkpoint_id"]),
    ).fetchone()
    assert applied is not None and "compact_anchor" not in applied[0]
    assert model.calls == 1
