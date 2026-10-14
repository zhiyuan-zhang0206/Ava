"""Wiring hook: asyncio's child watcher reaps each unit leader before root reads its birth."""

import asyncio
from typing import Any

spawn = asyncio.create_subprocess_exec


async def reaped_first(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    proc = await spawn(*args, **kwargs)
    await proc.wait()
    return proc


def build(context: Any) -> list[object]:
    del context
    asyncio.create_subprocess_exec = reaped_first  # pyright: ignore[reportAttributeAccessIssue]
    return []
