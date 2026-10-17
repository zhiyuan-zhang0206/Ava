"""Independent notification and best-effort browser/shell cleanup after hosted death."""

import asyncio
from collections.abc import Awaitable, Callable

from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.log import logger
from base.paths import meiva_home
from meiva.mcps import release_agent_chrome_pages


async def _release_default_chrome_pages(agent_id: int) -> bool:
    return await release_agent_chrome_pages(agent_id, meiva_home())


async def release_browser_pages(
    agent_id: int,
    *,
    page_releaser: Callable[[int], Awaitable[object]] = _release_default_chrome_pages,
) -> None:
    """Best-effort close of this agent's browser-mcp pages after termination commits."""
    try:
        released = await page_releaser(agent_id)
        if not released:
            logger.debug(
                "agent {agent_id} browser-page release skipped (no service / failed)",
                agent_id=agent_id,
            )
    except Exception:
        logger.opt(exception=True).warning(
            "agent {agent_id} browser-page release failed (non-fatal)",
            agent_id=agent_id,
        )


async def finish_termination(
    bus: EventBus,
    agent_id: int,
    cutoff: int | None,
    killer: Callable[[int, int], None] | None,
    *,
    page_releaser: Callable[[int], Awaitable[object]] = _release_default_chrome_pages,
) -> None:
    """Attempt every post-commit effect and preserve the first failure, including cancellation."""
    primary_error: BaseException | None = None
    try:
        await publish_agent_updated(bus, agent_id)
    except BaseException as exc:
        primary_error = exc
    try:
        await release_browser_pages(agent_id, page_releaser=page_releaser)
    except BaseException as browser_error:
        if primary_error is None:
            primary_error = browser_error
        else:
            primary_error.add_note(f"Post-commit browser cleanup also failed: {browser_error!r}")
            logger.opt(exception=browser_error).error(
                "termination committed but browser cleanup also failed after publication failure",
                agent_id=agent_id,
            )
    try:
        if cutoff is not None and killer is not None:
            await asyncio.to_thread(killer, agent_id, cutoff)
    except BaseException as cleanup_error:
        if primary_error is None:
            raise
        primary_error.add_note(f"Post-commit shell cleanup also failed: {cleanup_error!r}")
        logger.opt(exception=cleanup_error).error(
            "termination committed but shell cleanup also failed after an earlier failure",
            agent_id=agent_id,
            cutoff=cutoff,
        )
    if primary_error is not None:
        raise primary_error
