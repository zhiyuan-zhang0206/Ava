"""Runner-side spawn validation and repeatable dispatcher wake."""

from __future__ import annotations

import asyncio

from psycopg_pool import ConnectionPool

from ops.agents import latest_checkpoint_id
from ops.rpc_schemas import LaunchAgentRequest, SpawnAgentRequest, SpawnedAgent
from shared.agents import ForkSourceEmpty
from shared.config import settings
from shared.db import insert_inbound_message, publish_inbound_wake
from shared.machine import machine_name


async def launch_agent_op(body: LaunchAgentRequest, db_pool: ConnectionPool) -> SpawnedAgent:
    """Validate a gateway-created row and wake its host without changing identity.

    The legacy prompt branch supports an old gateway during a rolling update.
    New gateways send an attempt ID and an already-committed inbound instead.
    No failure here terminates the row: the gateway records launch failure and
    the pending scan can still deliver the committed first prompt.
    """
    # Lazy imports: both homes (ops_lifecycle, moving to ops_events with the
    # Task #1999 split) re-export THIS cluster, so a module-level import would
    # be circular in either merge order.
    from ops.ops_lifecycle import publish_inbound_arrived
    from shared.lm.factory import validate_model_config

    await asyncio.to_thread(validate_model_config, model=settings.lm.llm_model, config=body.config)
    if body.launch_attempt_id is not None:
        await asyncio.to_thread(_validate_launch_row, db_pool, body)
    elif body.prompt is not None:
        # Old gateway compatibility only. New requests never send a prompt.
        assert body.prompt_source is not None  # narrowed by the caller  # noqa: S101
        prompt = body.prompt
        if body.label:
            prompt = f"{prompt}\n\nYour label has been set to {body.label}."
        iid = await asyncio.to_thread(
            _insert_prompt_blocking, db_pool, body.agent_id, prompt, body.prompt_source
        )
        await publish_inbound_arrived(body.agent_id, iid, "chat", body.prompt_source, prompt)
    publish_inbound_wake(body.agent_id, "0")
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


def _spawn_prechecks_blocking(body: SpawnAgentRequest, db_pool: ConnectionPool) -> str | None:
    """Sync spawn pre-checks — via to_thread: model-config validation (may read
    provider API keys) + fork checkpoint lookup. Returns the fork checkpoint
    (None for a plain spawn)."""
    # Validate model config before any DB work — defense-in-depth on top of the
    # gateway-side check in post_agents. The gateway may be on a different machine
    # without API keys; the runner always has its own settings and is authoritative.
    from shared.lm.factory import validate_model_config

    validate_model_config(model=settings.lm.llm_model, config=body.config)
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
    db_pool: ConnectionPool, agent_id: int, prompt: str, source: str
) -> int:
    """Sync first-prompt insert for an old gateway during a rolling update."""
    with db_pool.connection() as conn:
        return insert_inbound_message(conn, agent_id, prompt, source=source)


# ─── lifecycle: terminate / resurrect / restart ─────────────────────────────
