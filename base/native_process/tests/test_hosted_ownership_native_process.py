"""The hosted incarnation context is task-local and copies into worker threads."""

from uuid import uuid4

from base.native_process.runtime_incarnation import RuntimeIncarnation, current_incarnation
from base.native_process.turn_identity import bind_turn_identity


async def test_hosted_incarnation_context_is_task_local_and_copies_to_thread() -> None:
    import asyncio

    async def read(agent_id: int) -> RuntimeIncarnation:
        original = RuntimeIncarnation(agent_id, uuid4(), uuid4())
        with bind_turn_identity(agent_id, incarnation=original):
            await asyncio.sleep(0)
            assert await asyncio.to_thread(current_incarnation, agent_id) == original
            return original

    first, second = await asyncio.gather(read(1), read(2))
    assert first != second
    assert current_incarnation(1) is None
