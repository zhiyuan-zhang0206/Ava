"""Concurrent tasks and worker threads retain their explicit original admissions."""

import asyncio
from uuid import uuid4

from base.native_process.runtime_incarnation import RuntimeIncarnation


async def test_hosted_incarnation_is_retained_by_each_task_and_worker_thread() -> None:
    async def read(original: RuntimeIncarnation) -> RuntimeIncarnation:
        await asyncio.sleep(0)
        retained = await asyncio.to_thread(original.require_agent, original.agent_id)
        assert retained is original
        return retained

    first, second = RuntimeIncarnation(1, uuid4(), uuid4()), RuntimeIncarnation(2, uuid4(), uuid4())
    returned = await asyncio.gather(read(first), read(second))
    assert returned == [first, second]
    assert first != second
