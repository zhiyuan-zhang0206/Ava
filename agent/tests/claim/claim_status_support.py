"""Claim-node test support shared by the topic files: agent status writes and waits, the hosted-owner fixture, the committed-publish filter and the compact-tail reader."""

import asyncio
from dataclasses import replace
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from psycopg_pool import AsyncConnectionPool

import ava
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.lm.catalog import ModelCatalog
from tests.fixtures.units import spawn_agent


def _committed_publishes(pub: MagicMock) -> list[dict]:
    """Filter event_publisher.emit call_args to the InboundCommitted payload list.

    node_lifecycle emits a timeline_snapshot on every enter; we only want the
    "InboundCommitted emit" behavior here, so filter out the noise.
    """
    import json

    return [
        json.loads(c.args[0])
        for c in pub.emit.call_args_list
        if json.loads(c.args[0]).get("role") == "inbound_committed"
    ]


async def _await_status(pool: AsyncConnectionPool, agent_id: int, expected: str) -> None:
    """Poll `agents_meta.status` on `pool` until it equals `expected`.

    The restart arm flips status through `ctx.ops_pool` (= `aops_pool`); read it
    back on the same pool the flip was written on. Raises if `expected` is not
    reached in 2s, reporting the observed status trail.

    (This helper once carried a heavy CI-flake forensic dump for an intermittent
    `idling != restarting`. That flake was a reused-id collision, killed at the
    source by the monotonic-id contract — see
    docs/decisions/engineering/tooling/2026-06-30-monotonic-test-ids.md — so
    the dump is gone; the status trail is enough for any residual failure.)
    """
    seen: list[object] = []
    row: tuple[object, ...] | None = None
    for _ in range(250):
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT status FROM agents_meta WHERE id = %s", (agent_id,))
            row = await cur.fetchone()
        current = row[0] if row is not None else None
        if not seen or seen[-1] != current:
            seen.append(current)
        if current == expected:
            return
        await asyncio.sleep(0.02)
    actual = row[0] if row is not None else None
    raise AssertionError(
        f"agent {agent_id} status {actual!r} != {expected!r} after 5s (status trail: {seen})"
    )


def _set_agent_status(db: psycopg.Connection, agent_id: int, status: str) -> None:
    """UPDATE agents_meta.status with rowcount assertion.

    Fail-fast on 0 rows — a silent no-op UPDATE (wrong / nonexistent agent_id)
    would otherwise surface much later as a baffling ``assert <spawn-value> ==
    <intended>`` status mismatch far from its cause.
    """
    with db.cursor() as cur:
        cur.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (status, agent_id))
        assert cur.rowcount == 1, (
            f"_set_agent_status: agent {agent_id} not updated (rowcount={cur.rowcount})"
        )
    db.commit()


@pytest.fixture
async def running_agent(
    aops_pool: AsyncConnectionPool,
    database: Database,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
):
    """Admit a real hosted owner and bind it throughout each dispatch test."""
    from uuid import uuid4

    from agent.ownership.hosted import admit_hosted_runtime
    from base.cluster.machine import machine_name

    agent_id = spawn_agent(catalog=model_catalog, authority=config_authority)
    incarnation = await admit_hosted_runtime(
        aops_pool,
        agent_id,
        machine_name(),
        uuid4(),
        expected_from="idling",
        db=database,
    )
    assert incarnation is not None
    ava.context = replace(ava.context, original_incarnation=incarnation)
    yield lambda: agent_id


def _compact_tail(update):
    """Assert the transport a compaction now uses — the window is cleared and
    rebuilding the standing head is handed to `init_context` — and return the
    parked tail, which is what this claim batch decided belongs behind it."""
    from langchain_core.messages.modifier import RemoveMessage as _Rm

    msgs = update["messages"]
    assert len(msgs) == 1, f"expected the sentinel alone, got {len(msgs)} messages"  # pyright: ignore[reportUnknownArgumentType]
    assert isinstance(msgs[0], _Rm)
    assert msgs[0].id == REMOVE_ALL_MESSAGES  # pyright: ignore[reportUnknownMemberType]
    return update["context_reset"].tail  # pyright: ignore[reportUnknownMemberType]


def _events_with_role(pub: MagicMock, role: str) -> list[dict[str, Any]]:
    """Every event_publisher.emit payload for *role*, parsed, in emit order."""
    import json

    return [
        json.loads(c.args[0])
        for c in pub.emit.call_args_list
        if json.loads(c.args[0]).get("role") == role
    ]


def _pair_compact_cycles(pub: MagicMock) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair every compact_started with its exactly-one compact_finished.

    Task #3323's completeness invariant: a started run reaches exactly one
    terminal state (success / failure / replaced), otherwise the frontend's
    ticking block would hang forever. Returns (started, finished) pairs in
    start order.
    """
    started = _events_with_role(pub, "compact_started")
    finished = _events_with_role(pub, "compact_finished")
    by_id = {e["compact_id"]: e for e in finished}
    assert len(finished) == len(by_id), f"duplicate terminal for one run: {finished}"
    assert {e["compact_id"] for e in started} == set(by_id), (
        f"unpaired compact runs: started={started}, finished={finished}"
    )
    return [(s, by_id[s["compact_id"]]) for s in started]
