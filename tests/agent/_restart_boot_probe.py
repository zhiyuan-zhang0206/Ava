"""Disposable CI child: exercise real admission crash boundaries, never an LLM."""

import asyncio
import os
import sys
from pathlib import Path

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent import _starting, lifecycle_observe, session_admission
from shared.config import settings
from shared.db import PG_KEEPALIVE_KWARGS
from shared.runtime_incarnation import RuntimeIncarnation

agent_id, command_id = int(sys.argv[1]), int(sys.argv[2])
artifact_directory, fault = Path(sys.argv[3]), sys.argv[4]
session_admission.run_dir = lambda: artifact_directory
original_publish = session_admission.publish_admitted_session
original_observe = lifecycle_observe.observe_process_admission
original_bind = _starting.bind_process_incarnation


def publish(incarnation: RuntimeIncarnation) -> None:
    if fault == "before_record":
        os._exit(71)
    original_publish(incarnation)
    if fault == "after_record":
        os._exit(72)


def observe(connection: psycopg.Connection, incarnation: RuntimeIncarnation) -> None:
    original_observe(connection, incarnation)
    if fault == "before_commit":
        os._exit(73)


def bind(incarnation: RuntimeIncarnation) -> None:
    if fault == "after_commit":
        os._exit(74)
    original_bind(incarnation)


session_admission.publish_admitted_session = publish
lifecycle_observe.observe_process_admission = observe
_starting.bind_process_incarnation = bind
if fault == "resurrect":
    _starting.claim_agent_row(agent_id, resurrect_command_id=command_id)
else:
    _starting.claim_agent_row(agent_id, restart_command_id=command_id)
assert "agent.loop" not in sys.modules, "runtime work imported before admission"


async def claim() -> None:
    from agent.db import claim_inbound_batch

    async with AsyncConnectionPool[psycopg.AsyncConnection](
        settings.data_plane.db_url,
        min_size=1,
        max_size=1,
        open=False,
        kwargs=PG_KEEPALIVE_KWARGS,
    ) as pool:
        if fault == "apply-restart-exit":
            from agent.lifecycle_apply import apply_process_lifecycle
            from shared.db import insert_inbound_message
            from shared.db_transaction import async_write_transaction

            with psycopg.connect(settings.data_plane.db_url) as conn:
                next_command = insert_inbound_message(conn, agent_id, "", "self", kind="restart")
            batch = await claim_inbound_batch(pool, agent_id)
            assert [item.id for item in batch] == [next_command]
            async with async_write_transaction(pool) as conn:
                assert await apply_process_lifecycle(conn, agent_id, next_command)
            sys.stdout.write(f"RESTART_APPLIED {next_command}\n")
            return
        batch = await claim_inbound_batch(pool, agent_id)
        sys.stdout.write(f"EXECUTION_ALLOWED {[item.id for item in batch]}\n")


asyncio.run(claim())
