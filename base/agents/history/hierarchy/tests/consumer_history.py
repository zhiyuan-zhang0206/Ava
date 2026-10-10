"""History and durable job views used by chunk consumer tests."""

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from psycopg_pool import AsyncConnectionPool

from base.agents.history.checkpoint import FullHistory


def notes_history() -> FullHistory:
    """The same four messages, but framework notes."""
    body: list[BaseMessage] = [
        HumanMessage(content=f"m{i}", id=f"m{i}", additional_kwargs={"ava_msg_type": "system_note"})
        for i in range(4)
    ]
    return FullHistory(body, (SystemMessage(content="head"),), (0,))


async def job_spans(pool: AsyncConnectionPool) -> list[tuple]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT status, start_index, end_index FROM understanding_chunk_jobs ORDER BY id"
        )
        return await cur.fetchall()


def inbound_history() -> FullHistory:
    body: list[BaseMessage] = [
        HumanMessage(
            content=f"m{i}",
            id=f"m{i}",
            additional_kwargs={
                "ava_msg_type": "inbound",
                "ava_source": "user",
                "ava_created_at": f"2026-10-05T0{i}:00:00+00:00",
            },
        )
        for i in range(4)
    ]
    return FullHistory(body, (SystemMessage(content="head"),), (0,))
