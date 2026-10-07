"""Native graph cancel observation and its metadata-serialized claim boundary."""

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.ownership.inbound import lock_inbound_owner
from base.agents.incarnation.native_work import activate_work, load_work
from base.agents.incarnation.native_work_models import NativeCancelMarker, NativeWorkTarget
from base.agents.messages.native_cancel import pending_native_cancel
from base.db.transaction import async_write_transaction
from base.native_process.runtime_incarnation import current_incarnation
from base.native_process.turn_identity import current_native_work_id


async def bound_native_cancel(
    conn: psycopg.AsyncConnection, agent_id: int
) -> NativeCancelMarker | None:
    """Read only the exact original work bound to this native continuation."""
    work_id = current_native_work_id()
    incarnation = current_incarnation(agent_id)
    if work_id is None or incarnation is None:
        return None
    work = await load_work(conn, work_id)
    if work is None or (work.target.agent_id, work.target.generation, work.target.owner) != (
        agent_id,
        incarnation.generation,
        incarnation.owner,
    ):
        return None
    return await pending_native_cancel(conn, work.target)


async def observe_bound_cancel(
    pool: AsyncConnectionPool, agent_id: int
) -> NativeCancelMarker | None:
    if current_native_work_id() is None:
        return None
    async with pool.connection() as conn:
        return await bound_native_cancel(conn, agent_id)


async def activate_routed_work(pool: AsyncConnectionPool, target: NativeWorkTarget | None) -> None:
    if target is None:
        return
    if current_native_work_id() != target.work_id:
        raise RuntimeError("native graph state differs from its bound invocation")
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, target.agent_id)
        await activate_work(conn, target)


def halt_for_native_cancel(marker: NativeCancelMarker) -> dict[str, object]:
    """One halt transition with exact attribution; acknowledgement belongs to host."""
    return {"halted": True, "turn_active": False, "turn_idle": True, "native_cancel": marker}
