"""A compact permit linearizes before late cancel; native closure is never overwritten."""

from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool

from agent.tests.claim.test_inbound_ownership import _insert
from base.lm.catalog import ModelCatalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.invocation.compact import apply as compact_apply
from services.agent_runner.agent_host.invocation.compact.checkpoint import cold_reader
from services.agent_runner.agent_host.tests.guarded_compact.admission import AcceptedHost, admit
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel
from tests.fixtures.model_catalog import AddBindings


def accept_cancel(client: TestClient, accepted: AcceptedHost) -> dict[str, Any]:
    observed = client.get(accepted.path + "/native-work", headers=accepted.headers)
    assert observed.status_code == 200, observed.text
    cancelled = client.post(
        accepted.path + "/cancel-work", json=observed.json(), headers=accepted.headers
    )
    assert cancelled.status_code == 200, cancelled.text
    return cancelled.json()


@pytest.mark.parametrize("stage", ["permit", "flush", "ack_lost"])
async def test_late_cancel_closes_after_compact_and_replay_cannot_overwrite(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    stage: str,
    model_catalog: ModelCatalog,
) -> None:
    model = SummaryModel(responses=["Original source summary. " * 100])
    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog, {"gpt-": replace(binding, build_single_attempt=lambda _: model)}
    )
    accepted = await admit(db_conn, aops_pool, client, monkeypatch, catalog=model_catalog)
    cancelled: list[dict[str, Any]] = []
    original = getattr(
        compact_apply,
        {"permit": "authorize", "flush": "flush_checkpoint", "ack_lost": "acknowledge"}[stage],
    )

    async def inject(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        if not cancelled and (stage != "ack_lost" or result is True):
            cancelled.append(accept_cancel(client, accepted))
            if stage == "ack_lost":
                raise psycopg.OperationalError("original application ACK response lost")
        return result

    monkeypatch.setattr(
        compact_apply,
        {"permit": "authorize", "flush": "flush_checkpoint", "ack_lost": "acknowledge"}[stage],
        inject,
    )
    await accepted.host.run_turn(accepted.agent)
    status = accepted.status(client)
    assert status["outcome"] == "applied" and model.calls == 1 and len(cancelled) == 1
    closure = db_conn.execute(
        "SELECT outcome,checkpoint_id FROM native_cancel_commands WHERE id=%s",
        (cancelled[0]["command_id"],),
    ).fetchone()
    assert closure is not None and closure[0] == "applied" and closure[1]
    persisted = await cold_reader(accepted.saver).aget_tuple(
        {
            "configurable": {
                "thread_id": str(accepted.agent),
                "checkpoint_id": closure[1],
            }
        }
    )
    assert persisted is not None
    assert (
        str(persisted.checkpoint["channel_values"]["native_cancel"].command_id)
        == cancelled[0]["command_id"]
    )
    assert (
        client.post(
            accepted.path + "/compact-history", json=accepted.target, headers=accepted.headers
        ).json()
        == accepted.acceptance
    )
    _insert(db_conn, accepted.agent)
    await accepted.host.run_turn(accepted.agent)
    assert model.calls == 1 and len(accepted.ordinary) == 2
