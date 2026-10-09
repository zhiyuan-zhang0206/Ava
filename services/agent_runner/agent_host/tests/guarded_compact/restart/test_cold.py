"""A dead closed compact executor cannot replay restart against its successor."""

import asyncio
from dataclasses import replace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from agent.tests.claim.test_inbound_ownership import _insert
from base.agents.incarnation.native_restart_models import NativeRestartRequest
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.messages.native_restart import accept_native_restart, native_restart_progress
from base.config import settings
from base.lm.catalog import ModelCatalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel, make_host
from services.agent_runner.agent_host.tests.guarded_compact.test_dead_host import (
    prepare_agent,
    read_child,
    start_child,
)
from tests.fixtures.model_catalog import AddBindings


@pytest.mark.parametrize("fence", ["target_replaced", "resurrect", "force_terminate"])
async def test_released_sigkill_original_restart_is_no_effect_not_successor_restart(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    fence: str,
    model_catalog: ModelCatalog,
) -> None:
    agent = prepare_agent(db_conn)
    secret = "guarded-compact-test-secret"  # noqa: S105 -- isolated credential
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(settings.gateway, "auth_middleware_enabled", True)
    headers = {
        "Authorization": f"Bearer {secret}",
        "Idempotency-Scope": "principal-v1",
        "Idempotency-Key": str(uuid4()),
    }
    path = f"/api/keyed/v1/agents/{agent}"
    child = start_child(db_conn.info.dsn, agent, "released")
    try:
        target = await read_child(child)
        compact = client.post(path + "/compact-history", json=target, headers=headers)
        assert compact.status_code == 202, compact.text
        assert child.stdin is not None
        child.stdin.write("compact\n")
        child.stdin.flush()
        assert await read_child(child) == {"stage": "prepared", "provider_calls": 1}
        active = client.get(path + "/native-work", headers=headers)
        assert active.status_code == 200, active.text
        original = NativeWorkTarget.model_validate(active.json())
        with ConnectionPool[psycopg.Connection](db_conn.info.dsn) as pool:
            restart = await asyncio.to_thread(
                accept_native_restart,
                pool,
                "closed-restart",
                agent,
                NativeRestartRequest(target=original),
                lambda _: None,
                catalog=model_catalog,
                llm_override=settings.lm.llm_override,
                default_model=settings.lm.llm_model,
            )
            child.stdin.write("close\n")
            child.stdin.flush()
            assert await read_child(child) == {"stage": "released", "provider_calls": 1}
            status = client.get(
                path + "/compact-commands/" + compact.json()["command_id"], headers=headers
            ).json()
            assert status["outcome"] == "applied" and status["continuation_released"]
            if fence != "target_replaced":
                supersede_original(db_conn, agent, fence)
            assert db_conn.execute(
                "SELECT native_work_id FROM agents_meta WHERE id=%s", (agent,)
            ).fetchone() == (original.work_id,)
            child.kill()
            await asyncio.wait_for(asyncio.to_thread(child.wait), 10)
            chat = _insert(db_conn, agent)
            model = SummaryModel(responses=["MUST NOT GENERATE" * 100])
            binding = model_catalog.bindings["gpt-"]
            model_catalog = add_bindings(
                model_catalog, {"gpt-": replace(binding, build_single_attempt=lambda _: model)}
            )
            ordinary: list[object] = []
            host, _, _ = await make_host(
                aops_pool,
                agent,
                100,
                ordinary,
                monkeypatch,
                seed_history=False,
                catalog=model_catalog,
            )
            await host.run_turn(agent)
            progress = await asyncio.to_thread(
                native_restart_progress, pool, agent, restart.command_id
            )
            assert_no_effect(db_conn, progress, fence, original, model, ordinary, chat, agent)
    finally:
        if child.poll() is None:
            child.kill()
        await asyncio.to_thread(child.communicate, timeout=10)


def supersede_original(conn: psycopg.Connection, agent: int, fence: str) -> None:
    from base.agents.incarnation.lifecycle_acceptance import (
        supersede_lifecycle_for_force,
        supersede_lifecycle_for_resurrect,
    )

    with conn.transaction():
        if fence == "force_terminate":
            forced = conn.execute(
                "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'','terminate','user') RETURNING id",
                (agent,),
            ).fetchone()
            assert forced is not None
            conn.execute(
                "UPDATE agents_meta SET last_force_terminate_inbound_id=%s WHERE id=%s",
                (forced[0], agent),
            )
            supersede_lifecycle_for_force(conn, agent, forced[0])
        # A later explicit resurrection supersedes the force's still-unapplied
        # source; the immutable original restart retains its first no-effect.
        row = conn.execute(
            "INSERT INTO inbound_messages(agent_id,content,kind,source) VALUES(%s,'','resurrect','user') RETURNING id",
            (agent,),
        ).fetchone()
        assert row is not None
        supersede_lifecycle_for_resurrect(conn, agent, row[0])
    conn.commit()


def assert_no_effect(
    db_conn: psycopg.Connection,
    progress: Any,
    fence: str,
    original: NativeWorkTarget,
    model: SummaryModel,
    ordinary: list[object],
    chat: int,
    agent: int,
) -> None:
    assert progress is not None and progress.outcome == "superseded"
    assert progress.reason == fence and progress.applied_at is None
    assert progress.acceptance.target == original and model.calls == 0
    assert len(ordinary) == 1 and str(ordinary[0]) != str(original.work_id)
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (chat,)
    ).fetchone() == ("done",)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s AND kind='restart'",
        (agent,),
    ).fetchone() == (1,)
