"""Real service task scopes for IM lifecycle tests."""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager


@asynccontextmanager
async def owned_tasks() -> AsyncGenerator[asyncio.TaskGroup]:
    """Cancel service-long loops when a test's owning scope finishes."""
    with contextlib.suppress(asyncio.CancelledError):
        async with asyncio.TaskGroup() as tasks:
            yield tasks
            raise asyncio.CancelledError
