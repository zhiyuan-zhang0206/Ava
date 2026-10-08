"""Current admitted authority over the exact ended source, before a generation exists."""

import psycopg

from base.agents.compaction.models import CompactAcceptance, CompactHeldError
from base.agents.incarnation.native_work import load_work, receiver
from base.agents.incarnation.resources import IncarnationResources, decode_resources
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def require_source_receiver(
    conn: psycopg.AsyncConnection,
    acceptance: CompactAcceptance,
    incarnation: RuntimeIncarnation,
) -> str:
    """Lock first, then validate source pointer, actual receiver and closed managed resources."""
    await conn.execute("SELECT id FROM agents_meta WHERE id=%s FOR UPDATE", (incarnation.agent_id,))
    row = await (
        await conn.execute(
            "SELECT machine,runtime_generation,runtime_owner,incarnation_resources,native_work_id "
            "FROM agents_meta WHERE id=%s AND runtime_kind='hosted' "
            "AND lease_expires_at>clock_timestamp() AND NOT EXISTS ("
            "SELECT 1 FROM agent_impersonations p WHERE p.agent_id=agents_meta.id AND p.status='active')",
            (incarnation.agent_id,),
        )
    ).fetchone()
    target = acceptance.target.source
    source = await load_work(conn, target.work_id, lock=True)
    ended = await (
        await conn.execute(
            "SELECT 1 FROM native_graph_work WHERE id=%s AND phase='settled' AND ended_at IS NOT NULL",
            (target.work_id,),
        )
    ).fetchone()
    if row is None or source is None or source.target != target or ended is None:
        raise CompactHeldError("compact source lacks its original ended work")
    evidence = decode_resources(row[3])
    if (
        target.agent_id != incarnation.agent_id
        or (row[1], row[2]) != (incarnation.generation, incarnation.owner)
        or receiver(source) != (row[0], incarnation.generation, incarnation.owner)
        or row[4] != target.work_id
        or not isinstance(evidence, IncarnationResources)
        or (evidence.generation, evidence.owner) != (incarnation.generation, incarnation.owner)
        or evidence.host_process is None
        or evidence.frozen_by is not None
        or evidence.requests
    ):
        raise CompactHeldError("compact source lost its exact admitted closed-resource receiver")
    return row[0]
