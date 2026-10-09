"""Actual host quiescence produces compact observations; a status row cannot."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.state import CompactState
from base.agents.compaction.history import ADMISSION_SQL, PENDING_HISTORY_SQL
from base.agents.compaction.models import CompactTarget
from base.agents.incarnation.native_work import NativeWorkRecord, load_work, managed_resources
from base.agents.incarnation.native_work_models import NativeWorkPhase, NativeWorkTarget
from base.agents.incarnation.resources import IncarnationResources, decode_resources
from base.agents.observation.db_wait import DatabaseWaits
from base.config import settings
from base.config.agent_pins import resolve_agent_config_pins
from base.db.transaction import async_write_transaction
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import model_catalog
from base.log import logger
from base.native_process.turn_identity import HostedTurnResources
from services.agent_runner.agent_host.db_recovery import recover_database
from services.agent_runner.agent_host.invocation.compact.apply import CompactGraph
from services.agent_runner.agent_host.invocation.compact.checkpoint import cold_reader
from services.agent_runner.agent_host.invocation.compact.lifecycle import settle_original_restart
from services.agent_runner.agent_host.wake_screening import read_stored_config


async def produce_source(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    agent_id: int,
    owner: UUID,
    resources: HostedTurnResources,
) -> None:
    """Called only after the actual turn task ended, inside its serialized pump."""
    if resources.unresolved or any(not task.done() for task in resources.completions):
        return
    model = await _source_model(pool, agent_id)
    if model is None:
        return
    reader = cold_reader(saver)
    snapshot = await reader.aget_tuple({"configurable": {"thread_id": str(agent_id)}})
    if snapshot is None or snapshot.pending_writes:
        return
    checkpoint = snapshot.checkpoint
    versions = checkpoint["channel_versions"]
    if "messages" not in versions:
        return
    values = checkpoint["channel_values"]
    if values.get("turn_idle") is not True or values.get("turn_active") is not False:
        return
    compact = CompactState.model_validate(checkpoint["channel_values"].get("compact", {}))
    async with async_write_transaction(pool) as conn:
        closed = await _closed_source(conn, agent_id, owner)
        if closed is None or not await _input_is_quiet(conn, agent_id, checkpoint["id"]):
            return
        work, evidence = closed
        target = CompactTarget(
            protocol=1,
            observation_id=uuid4(),
            source=work.target,
            checkpoint_id=checkpoint["id"],
            checkpoint_ns="",
            messages_version=str(versions["messages"]),
            compact_channel_version=None if "compact" not in versions else str(versions["compact"]),
            segment_version=compact.version,
            model=model,
        )
        await conn.execute(
            "INSERT INTO native_compact_observations(id,agent_id,work_id,target,resources) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                target.observation_id,
                agent_id,
                work.target.work_id,
                Jsonb(target.model_dump(mode="json")),
                Jsonb(evidence.model_dump(mode="json")),
            ),
        )


async def finish_compact_pump(
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: CompactGraph,
    agent_id: int,
    owner: UUID,
    resources: HostedTurnResources,
) -> None:
    """Actual closed pump is the only producer; failures preserve pending intent."""
    from services.agent_runner.agent_host.invocation.compact.recovery import settle_quiescent

    try:
        await settle_quiescent(pool, saver, graph, agent_id, owner, resources)
        await produce_source(pool, saver, agent_id, owner, resources)
    except Exception as exc:
        logger.warning(
            "guarded compact proof remains unavailable",
            event="native_compact_proof_gap",
            agent_id=agent_id,
            error_type=type(exc).__name__,
        )


async def _source_model(pool: AsyncConnectionPool, agent_id: int) -> str | None:
    stored = await read_stored_config(pool, agent_id)
    if stored is None or settings.lm.llm_override:
        return None
    slices = AgentSlices.resolve(
        resolve_agent_config_pins(stored.config_overlay, stored.birth_config),
    )
    model = slices.brain.llm_model
    if not isinstance(model, str) or not any(
        model.startswith(prefix) and binding.build_single_attempt is not None
        for prefix, binding in model_catalog().bindings.items()
    ):
        return None
    return model


async def _closed_source(
    conn: psycopg.AsyncConnection, agent_id: int, owner: UUID
) -> tuple[NativeWorkRecord, IncarnationResources] | None:
    await conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (agent_id,))
    row = await (
        await conn.execute(
            "SELECT native_work_id,incarnation_resources FROM agents_meta WHERE id=%s "
            "AND runtime_owner=%s AND status='idling' AND runtime_kind='hosted' "
            "AND lease_expires_at>clock_timestamp() FOR UPDATE",
            (agent_id, owner),
        )
    ).fetchone()
    if row is None or row[0] is None:
        return None
    work = await load_work(conn, row[0], lock=True)
    if work is None or work.phase != NativeWorkPhase.SETTLED:
        return None
    ended = await (
        await conn.execute(
            "SELECT 1 FROM native_graph_work WHERE id=%s AND ended_at IS NOT NULL",
            (work.target.work_id,),
        )
    ).fetchone()
    if ended is None:
        return None
    evidence = decode_resources(row[1])
    if (
        not managed_resources(row[1], work.target)
        or not isinstance(evidence, IncarnationResources)
        or evidence.requests
    ):
        return None
    return work, evidence


async def _input_is_quiet(conn: psycopg.AsyncConnection, agent_id: int, checkpoint_id: str) -> bool:
    latest = await (
        await conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id=%s AND checkpoint_ns='' "
            "ORDER BY checkpoint_id DESC LIMIT 1",
            (str(agent_id),),
        )
    ).fetchone()
    pending_write = await (await conn.execute(PENDING_HISTORY_SQL, (str(agent_id),) * 2)).fetchone()
    if latest != (checkpoint_id,) or pending_write is not None:
        return False
    pending = await (
        await conn.execute(
            "SELECT 1 FROM native_compact_commands WHERE agent_id=%s AND released_at IS NULL",
            (agent_id,),
        )
    ).fetchone()
    if pending is not None:
        return False
    admission = await (await conn.execute(ADMISSION_SQL, (agent_id,))).fetchone()
    return admission is None


async def finish_force_and_compact(
    force: Awaitable[bool],
    pool: AsyncConnectionPool,
    saver: AsyncPostgresSaver,
    graph: CompactGraph,
    agent_id: int,
    owner: UUID,
    resources: HostedTurnResources,
    bus: EventBus,
    drop_agent: Callable[[int], None],
    database_waits: DatabaseWaits,
    peek_lock: asyncio.Lock,
    *,
    work: NativeWorkTarget | None,
) -> None:
    """Both proof tails share the original shielded owned settlement task."""
    await force
    await finish_compact_pump(pool, saver, graph, agent_id, owner, resources)
    await settle_original_restart(
        pool,
        bus,
        agent_id,
        owner,
        drop_agent,
        lambda token: recover_database(
            pool=pool,
            checkpointer=saver,
            graph=graph,
            incarnation=token,
            database_waits=database_waits,
            peek_lock=peek_lock,
            work=work,
        ),
        resources=resources,
    )
