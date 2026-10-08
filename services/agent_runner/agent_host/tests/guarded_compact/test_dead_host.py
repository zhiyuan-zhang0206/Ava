"""Actual dead executor certificates recover frozen compact output, never another model call."""

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.tests.test_inbound_ownership import _agent, _insert
from base.agents.incarnation.resources import ResourceBirth
from base.config import settings
from base.lm.plugin_providers import model_catalog
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.invocation.compact.checkpoint import cold_reader
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel, make_host
from tests.fixtures.model_catalog import AddBindings


async def read_child(child: subprocess.Popen[str]) -> dict[str, Any]:
    assert child.stdout is not None
    line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 15)
    assert line, child.stderr.read() if child.stderr is not None else "child missing proof"
    return json.loads(line)


def start_child(dsn: str, agent: int, stage: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "services.agent_runner.agent_host.tests.guarded_compact.child"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "AVA_DB_URL": dsn,
            "AVA_REDIS_URL": settings.data_plane.redis_url,
            "OPENAI_API_KEY": "test-key",
            "AVA_TEST_COMPACT_AGENT": str(agent),
            "AVA_TEST_COMPACT_STAGE": stage,
        },
    )


def prepare_agent(db_conn: psycopg.Connection) -> int:
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
    return agent


@pytest.mark.parametrize("stage", ["prepared", "applying", "reset", "applied", "short"])
async def test_actual_sigkill_certified_successor_and_cancellation_order(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    stage: str,
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
    child = start_child(db_conn.info.dsn, agent, stage)
    try:
        accepted, active, cancelled = await kill_at_boundary(child, client, path, headers, stage)
        chat = _insert(db_conn, agent)
        model = SummaryModel(responses=["MUST NEVER GENERATE ANOTHER SUMMARY" * 100])
        binding = model_catalog().bindings["gpt-"]
        add_bindings({"gpt-": replace(binding, build_single_attempt=lambda _: model)})
        ordinary: list[object] = []
        host, saver, config = await make_host(
            aops_pool, agent, 100, ordinary, monkeypatch, seed_history=False
        )
        await host.run_turn(agent)
        status = client.get(
            path + "/compact-commands/" + accepted.json()["command_id"], headers=headers
        ).json()
        assert (
            status["outcome"]
            == {
                "prepared": "uncertain",
                "applying": "applied",
                "reset": "applied",
                "applied": "applied",
                "short": "rejected",
            }[stage]
        )
        assert status["continuation_released"]
        assert model.calls == 0
        assert_exact_closed(db_conn, active.json()["work_id"], cancelled.json()["command_id"])
        if stage == "prepared":
            assert ordinary == [] and db_conn.execute(
                "SELECT status FROM inbound_messages WHERE id=%s", (chat,)
            ).fetchone() == ("pending",)
            await host.run_turn(agent)
        assert len(ordinary) == 1
        assert model.calls == 0
        persisted = await cold_reader(saver).aget_tuple(config)
        assert persisted is not None
        has_original = any(
            "Original child summary." in str(m.content)
            for m in persisted.checkpoint["channel_values"]["messages"]
        )
        assert has_original == (stage in ("applying", "reset", "applied"))
    finally:
        if child.poll() is None:
            child.kill()
        await asyncio.to_thread(child.communicate, timeout=10)


def assert_exact_closed(db_conn: psycopg.Connection, work_id: str, command_id: str) -> None:
    proof = db_conn.execute(
        "SELECT transfer_chain,phase,ended_at FROM native_graph_work WHERE id=%s",
        (work_id,),
    ).fetchone()
    assert proof is not None and proof[0][0]["proof"]["reason"] == "exact_host_exit"
    assert proof[1] == "settled" and proof[2] is not None
    assert db_conn.execute(
        "SELECT outcome FROM native_cancel_commands WHERE id=%s",
        (command_id,),
    ).fetchone() == ("recovered_stopped",)


async def kill_at_boundary(
    child: subprocess.Popen[str],
    client: TestClient,
    path: str,
    headers: dict[str, str],
    stage: str,
) -> tuple[Any, Any, Any]:
    target = await read_child(child)
    accepted = client.post(path + "/compact-history", json=target, headers=headers)
    assert accepted.status_code == 202, accepted.text
    assert child.stdin is not None
    child.stdin.write("compact\n")
    child.stdin.flush()
    boundary = await read_child(child)
    assert boundary == {"stage": stage, "provider_calls": 1}
    active = client.get(path + "/native-work", headers=headers)
    assert active.status_code == 200, active.text
    cancelled = client.post(path + "/cancel-work", json=active.json(), headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    child.kill()
    await asyncio.wait_for(asyncio.to_thread(child.wait), 10)
    assert child.returncode is not None and child.returncode < 0
    return accepted, active, cancelled
