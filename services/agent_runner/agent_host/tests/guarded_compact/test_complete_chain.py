"""Real serialized host, HTTP admission, shared init-context and cold application proof."""

from dataclasses import replace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.tests.claim.test_inbound_ownership import _agent, _insert
from base.agents.incarnation.resources import ResourceBirth
from base.config import settings
from base.lm.catalog import ModelCatalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.invocation.compact.checkpoint import cold_reader
from services.agent_runner.agent_host.tests.guarded_compact.faults import install
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel, make_host
from tests.fixtures.model_catalog import AddBindings


@pytest.mark.parametrize("interval", [1, 100])
@pytest.mark.parametrize(
    "fault", ["none", "before_result", "after_result", "after_apply_checkpoint", "after_ack"]
)
async def test_real_host_http_once_summary_cold_ack_and_next_chat(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    interval: int,
    fault: str,
    model_catalog: ModelCatalog,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    agent = _agent(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s,config_overlay=%s WHERE id=%s",
        (
            Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")),
            Jsonb({"llm_model": "gpt-6.1-sol"}),
            agent,
        ),
    )
    db_conn.commit()
    summaries: list[str] = []
    generated_models: list[SummaryModel] = []

    def build_single(ctx: Any) -> SummaryModel:
        assert ctx.model == "gpt-6.1-sol"
        summaries.append(ctx.model)
        model = SummaryModel(responses=["Durable original summary. " * 100])
        generated_models.append(model)
        return model

    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog, {"gpt-": replace(binding, build_single_attempt=build_single)}
    )
    ordinary: list[object] = []

    host, saver, config = await make_host(
        aops_pool, agent, interval, ordinary, monkeypatch, catalog=model_catalog
    )
    await host.run_turn(agent)
    assert len(ordinary) == 1
    secret = "guarded-compact-test-secret"  # noqa: S105 -- isolated credential
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    headers = {
        "Authorization": f"Bearer {secret}",
        "Idempotency-Scope": "principal-v1",
        "Idempotency-Key": str(uuid4()),
    }
    path = f"/api/keyed/v1/agents/{agent}"
    observed = client.get(path + "/compact-target", headers=headers)
    assert observed.status_code == 200, observed.text
    accepted = client.post(path + "/compact-history", json=observed.json(), headers=headers)
    assert accepted.status_code == 202, accepted.text
    injected = install(monkeypatch, fault)
    await host.run_turn(agent)
    status = client.get(
        path + "/compact-commands/" + accepted.json()["command_id"], headers=headers
    )
    assert_original_completion(status, summaries, generated_models, ordinary, injected, fault)
    persisted = await cold_reader(saver).aget_tuple(config)
    assert persisted is not None
    values = persisted.checkpoint["channel_values"]
    assert_original_replaced(values)
    assert (
        client.post(path + "/compact-history", json=observed.json(), headers=headers).json()
        == accepted.json()
    )
    _insert(db_conn, agent)
    await host.run_turn(agent)
    assert len(ordinary) == 2
    assert str(ordinary[-1]) != status.json()["execution"]["work_id"]
    assert summaries == ["gpt-6.1-sol"]
    assert generated_models[0].calls == 1


def assert_original_replaced(values: dict[str, Any]) -> None:
    assert all(message.id != "source-history" for message in values["messages"])
    assert any("Durable original summary." in message.content for message in values["messages"])


def assert_original_completion(
    status: Any,
    summaries: list[str],
    generated_models: list[SummaryModel],
    ordinary: list[object],
    injected: list[str],
    fault: str,
) -> None:
    assert status.status_code == 200, status.text
    assert status.json()["outcome"] == "applied", status.text
    assert status.json()["checkpoint_id"]
    assert status.json()["result_available"]
    assert summaries == ["gpt-6.1-sol"]
    assert len(ordinary) == 1
    assert generated_models[0].calls == 1
    assert injected == ([] if fault == "none" else [fault])
