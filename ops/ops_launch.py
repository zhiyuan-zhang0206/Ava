"""Spawn launch ops — model validation, prompt delivery, and the launch itself.

Split out of `ops/ops_lifecycle.py` (Task #1999) when the lifecycle cluster
crossed the 800-line ceiling. Owns the runner-side half of a spawn (the gateway
created the row): model-config validation, the plain-spawn first-prompt
delivery, and the launch itself — a detached process in process mode, or a
prompt + wake for the agent-host dispatcher in hosted mode, where a failed
launch reclaims its own row (no restarter reaper exists there).
"""

from __future__ import annotations

import asyncio

from psycopg_pool import ConnectionPool

from ops import agent_launch, runner_mode
from ops.agents import latest_checkpoint_id
from ops.rpc_schemas import LaunchAgentRequest, SpawnAgentRequest, SpawnedAgent
from shared.agents import ForkSourceEmpty
from shared.config import settings
from shared.db import insert_inbound_message, publish_inbound_wake


async def launch_agent_op(body: LaunchAgentRequest, db_pool: ConnectionPool) -> SpawnedAgent:
    """Launch a PRE-CREATED agent row on this machine (Task #1236 follow-up).

    The gateway created the row (agents + agents_meta, as the main identity)
    and forwards only the launch here — this ops server runs as the
    least-privilege `ava_runner` role, which by design cannot INSERT agents.
    Everything this op does is within the runner role: model-config validation,
    the detached child launch (OS-level), the launch-confirm (agents_meta
    UPDATE), and the plain-spawn first prompt (inbound INSERT).
    """
    # Lazy imports: both homes (ops_lifecycle, moving to ops_events with the
    # Task #1999 split) re-export THIS cluster, so a module-level import would
    # be circular in either merge order.
    from ops.ops_lifecycle import publish_inbound_arrived
    from shared.lm.factory import validate_model_config

    if runner_mode.is_hosted():
        # Hosted: the row the gateway created IS the agent. No fork, no
        # launch-confirm (there is no pid to wait for — the claim CAS of the
        # dispatcher's turn task is the confirmation). The prompt INSERT below
        # publishes its own wake; the explicit wake at the end covers the fork,
        # whose inbounds are raw SQL with no publish, and gives a plain spawn
        # one cheap no-op turn that doubles as a spawn health check.
        #
        # Failure containment: a process-mode launch failure is backstopped by
        # the restarter's unclaimed-idling reaper, which hosted mode retires
        # (no restarter, and pid stays NULL for a hosted row's whole life, so
        # that reaper's predicate cannot exist here). The hosted launch must
        # therefore reclaim its own corpse: any failure after the row exists
        # marks it terminated (stamped 'launch-confirm', the same class the
        # process-mode launch confirm uses) so a half-launched spawn is a
        # visible dead agent instead of an idling row nothing will ever touch.
        # Validation runs INSIDE the reclaim for the same reason: the row
        # exists before validation does, and a failed validation left outside
        # would leak an idling row the heartbeat pokes into a prompt-less
        # zombie (no restarter reaper exists in hosted mode).
        try:
            await asyncio.to_thread(
                validate_model_config, model=settings.lm.llm_model, config=body.config
            )
            if body.prompt is not None:
                assert body.prompt_source is not None  # narrowed by the caller  # noqa: S101
                prompt = body.prompt
                if body.label:
                    prompt = f"{prompt}\n\nYour label has been set to {body.label}."
                iid = await asyncio.to_thread(
                    _insert_prompt_blocking, db_pool, body.agent_id, prompt, body.prompt_source
                )
                await publish_inbound_arrived(
                    body.agent_id, iid, "chat", body.prompt_source, prompt
                )
            publish_inbound_wake(body.agent_id, "0")
            return SpawnedAgent(id=body.agent_id)
        except Exception:
            # Lazy import: _force_mark_terminated lives in ops_lifecycle (moving
            # to ops_exit with the Task #1999 split), and a module-level import
            # would be circular (ops_lifecycle re-exports this cluster).
            from ops.ops_lifecycle import _force_mark_terminated

            await asyncio.to_thread(
                _force_mark_terminated,
                body.agent_id,
                db_pool,
                source="launch-confirm",
            )
            raise
    # Process mode: validate model config before launching — the runner owns
    # the LLM keys and is authoritative (defense-in-depth on top of the
    # gateway-side check). Off the event loop: may read provider API keys. A
    # failure here lands before the fork, with no process launched; the
    # restarter's unclaimed-idling reaper backstops the orphaned row.
    await asyncio.to_thread(validate_model_config, model=settings.lm.llm_model, config=body.config)
    # _launch_agent_process is synchronous (detached-process launch). Run it off
    # the event loop so a launch never blocks the ops dispatch loop and starves
    # concurrent requests (status probes / other ops) while it runs.
    await asyncio.to_thread(
        agent_launch._launch_agent_process,
        body.agent_id,
        body.config,
        birth_config=body.birth_config,
        confirm=False,
    )
    # Confirm the launched child claimed its row off this response path; a launch
    # that never claims is forced 'terminated' there (reaper backstops).
    agent_launch.schedule_launch_confirm(body.agent_id)
    if body.prompt is not None:
        assert body.prompt_source is not None  # narrowed by the caller  # noqa: S101
        prompt = body.prompt
        if body.label:
            prompt = f"{prompt}\n\nYour label has been set to {body.label}."
        iid = await asyncio.to_thread(
            _insert_prompt_blocking, db_pool, body.agent_id, prompt, body.prompt_source
        )
        await publish_inbound_arrived(body.agent_id, iid, "chat", body.prompt_source, prompt)
    return SpawnedAgent(id=body.agent_id)


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
    """Sync first-prompt inbound INSERT for a plain spawn — via to_thread."""
    with db_pool.connection() as conn:
        return insert_inbound_message(conn, agent_id, prompt, source=source)


# ─── lifecycle: terminate / resurrect / restart ─────────────────────────────
