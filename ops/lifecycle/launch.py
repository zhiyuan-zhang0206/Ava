"""Runner-side spawn validation and repeatable dispatcher wake."""

from __future__ import annotations

import asyncio

from psycopg_pool import ConnectionPool

from base.agents import ForkSourceEmpty
from base.agents.labels import spawn_prompt_with_label
from base.cluster.machine import machine_name
from base.config import settings
from base.config.agent_pins import resolve_agent_config_pins
from base.db import Database, insert_inbound_message, publish_inbound_wake
from base.events.live.bus import EventBus
from ops.agents import latest_checkpoint_id
from ops.rpc_schemas import LaunchAgentRequest, SpawnAgentRequest, SpawnedAgent


async def launch_agent_op(
    db: Database, bus: EventBus, body: LaunchAgentRequest, db_pool: ConnectionPool
) -> SpawnedAgent:
    """Validate a gateway-created row and wake its host without changing identity.

    The legacy prompt branch supports an old gateway during a rolling update.
    New gateways send an attempt ID and an already-committed inbound instead.
    No failure here terminates the row: the gateway records launch failure and
    the pending scan can still deliver the committed first prompt.
    """
    # Lazy import: the package door re-exports this module, so a module-level
    # import of the door would be circular.
    from base.lm.factory import validate_model_config
    from ops.lifecycle import publish_inbound_arrived

    await asyncio.to_thread(
        validate_model_config,
        model=settings.lm.llm_model,
        config=resolve_agent_config_pins(body.config, body.birth_config),
    )
    if body.launch_attempt_id is not None:
        await asyncio.to_thread(_validate_launch_row, db_pool, body)
    elif body.prompt is not None:
        # Old gateway compatibility only. New requests never send a prompt.
        assert body.prompt_source is not None  # narrowed by the caller  # noqa: S101
        prompt = spawn_prompt_with_label(body.prompt, body.label)
        iid = await asyncio.to_thread(
            _insert_prompt_blocking, db, bus, db_pool, body.agent_id, prompt, body.prompt_source
        )
        await publish_inbound_arrived(bus, body.agent_id, iid, "chat", body.prompt_source, prompt)
    publish_inbound_wake(db, bus, body.agent_id, "0")
    return SpawnedAgent(id=body.agent_id)


def _validate_launch_row(db_pool: ConnectionPool, body: LaunchAgentRequest) -> None:
    """A stale attempt cannot wake an identity now placed somewhere else."""
    with db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT machine, last_launch_attempt_id, status FROM agents_meta WHERE id=%s",
            (body.agent_id,),
        )
        row = cur.fetchone()
    if row is None or row != (machine_name(), body.launch_attempt_id, "idling"):
        raise ValueError(f"agent {body.agent_id} launch attempt is stale or misplaced")


def spawn_prechecks_blocking(body: SpawnAgentRequest, db_pool: ConnectionPool) -> str | None:
    """Sync spawn pre-checks — via to_thread: model-config validation (may read
    provider API keys) + fork checkpoint lookup. Returns the fork checkpoint
    (None for a plain spawn)."""
    from base.lm.model_config import validate_spawn_model_config

    with db_pool.connection() as conn, conn.cursor() as cur:
        validate_spawn_model_config(cur, body.config, body.fork_from)
    fork_checkpoint: str | None = None
    if body.fork_from is not None:
        with db_pool.connection() as conn, conn.cursor() as cur:
            fork_checkpoint = latest_checkpoint_id(cur, body.fork_from)
        if fork_checkpoint is None:
            raise ForkSourceEmpty(
                f"agent {body.fork_from} has no checkpoint — it may not have run any LLM/exec step yet"
            )
    if body.prompt is not None and body.prompt_source is None:
        raise RuntimeError("prompt_source missing despite schema validator")
    return fork_checkpoint


def _insert_prompt_blocking(
    db: Database, bus: EventBus, db_pool: ConnectionPool, agent_id: int, prompt: str, source: str
) -> int:
    """Sync first-prompt insert for an old gateway during a rolling update."""
    with db_pool.connection() as conn:
        return insert_inbound_message(conn, agent_id, prompt, source=source, database=db, bus=bus)


# ─── lifecycle: terminate / resurrect / restart ─────────────────────────────
