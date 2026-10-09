"""Native graph cancel observation and its metadata-serialized claim boundary."""

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.ownership.inbound import lock_inbound_owner
from base.agents.incarnation.native_work import activate_work, load_work
from base.agents.incarnation.native_work_models import NativeCancelMarker, NativeWorkTarget
from base.agents.messages.native_cancel import pending_native_cancel
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def bound_native_cancel(
    conn: psycopg.AsyncConnection,
    agent_id: int,
    *,
    incarnation: RuntimeIncarnation | None,
    work: NativeWorkTarget | None,
) -> NativeCancelMarker | None:
    """Read only the exact original work bound to this native continuation."""
    if work is None or incarnation is None:
        return None
    incarnation.require_agent(agent_id)
    record = await load_work(conn, work.work_id)
    if record is None or (
        record.target.agent_id,
        record.target.generation,
        record.target.owner,
    ) != (
        agent_id,
        incarnation.generation,
        incarnation.owner,
    ):
        return None
    return await pending_native_cancel(conn, record.target)


async def observe_bound_cancel(
    pool: AsyncConnectionPool,
    agent_id: int,
    *,
    incarnation: RuntimeIncarnation | None,
    work: NativeWorkTarget | None,
) -> NativeCancelMarker | None:
    if work is None:
        return None
    async with pool.connection() as conn:
        return await bound_native_cancel(conn, agent_id, incarnation=incarnation, work=work)


async def activate_routed_work(
    pool: AsyncConnectionPool,
    target: NativeWorkTarget | None,
    *,
    incarnation: RuntimeIncarnation | None,
    work: NativeWorkTarget | None,
) -> None:
    if target is None:
        return
    if work is None or work.work_id != target.work_id:
        raise RuntimeError("native graph state differs from its bound invocation")
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, target.agent_id, incarnation=incarnation)
        await activate_work(conn, target)


def halt_for_native_cancel(marker: NativeCancelMarker) -> dict[str, object]:
    """One halt transition with exact attribution; acknowledgement belongs to host."""
    return {"halted": True, "turn_active": False, "turn_idle": True, "native_cancel": marker}
