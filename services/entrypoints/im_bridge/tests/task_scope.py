import asyncio
import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager


@asynccontextmanager
async def owned_tasks() -> AsyncGenerator[asyncio.TaskGroup]:
    with contextlib.suppress(asyncio.CancelledError):
        async with asyncio.TaskGroup() as tasks:
            yield tasks
            raise asyncio.CancelledError
