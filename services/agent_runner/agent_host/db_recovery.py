"""Bound database-only phases; never retry a graph or retained continuation."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from psycopg_pool import PoolTimeout


@asynccontextmanager
async def database_phase() -> AsyncGenerator[None]:
    """Bound one database phase and preserve external cancellation."""
    try:
        async with asyncio.timeout(30.0):
            yield
    except TimeoutError as exc:
        raise PoolTimeout("host database phase timed out") from exc
