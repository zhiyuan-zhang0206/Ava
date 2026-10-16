from __future__ import annotations

from langchain_core.messages import AIMessage
from psycopg_pool import AsyncConnectionPool

from base.agents.history.hierarchy.chunks import ChunkCall, ChunkJob, write_chunk_calls
from base.agents.history.hierarchy.group import GroupCall
from base.agents.history.hierarchy.group_store import write_group_calls


async def test_nul_characters_in_the_chunk_record_are_stored_as_replacements(
    aops_pool: AsyncConnectionPool,
) -> None:
    job = ChunkJob(
        id=1,
        agent_id=5,
        compact_version=0,
        start_index=1,
        end_index=3,
        end_msg_id="m1",
        boundary_checkpoint_id=None,
        attempts=1,
    )
    call = ChunkCall(
        round=0,
        model="deepseek-flash",
        instruction="describe it\x00",
        prefix_len=3,
        start_offset=1,
        response=AIMessage(content=[{"type": "text", "text": "what\x00happened"}]),
        duration_ms=12.5,
        error=None,
    )
    await write_chunk_calls(aops_pool, job, [call])
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT instruction, content FROM understanding_chunk_calls")
        assert await cur.fetchall() == [
            ("describe it\ufffd", [{"type": "text", "text": "what\ufffdhappened"}])
        ]


async def test_nul_characters_in_the_group_record_are_stored_as_replacements(
    aops_pool: AsyncConnectionPool,
) -> None:
    call = GroupCall(
        round=0,
        model="deepseek-flash",
        request="prompt\x00",
        response=AIMessage(content="r\x00s"),
        duration_ms=5.0,
        problem=None,
        error=None,
    )
    await write_group_calls(aops_pool, 7, 1, "check", [1, 2], [call])
    async with aops_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT request, content FROM understanding_group_calls")
        assert await cur.fetchall() == [("prompt\ufffd", "r\ufffds")]
