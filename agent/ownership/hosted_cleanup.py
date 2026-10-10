"""Independent notification and best-effort shell cleanup after hosted death."""

import asyncio
from collections.abc import Callable

from base.events.live.announce import publish_agent_updated
from base.events.live.bus import EventBus
from base.log import logger


async def finish_termination(
    bus: EventBus,
    agent_id: int,
    cutoff: int | None,
    killer: Callable[[int, int], None] | None,
) -> None:
    """Attempt both post-commit effects while preserving a publication failure."""
    publication_error: BaseException | None = None
    try:
        await publish_agent_updated(bus, agent_id)
    except BaseException as exc:
        publication_error = exc
    try:
        if cutoff is not None and killer is not None:
            await asyncio.to_thread(killer, agent_id, cutoff)
    except BaseException as cleanup_error:
        if publication_error is None:
            raise
        publication_error.add_note(f"Post-commit shell cleanup also failed: {cleanup_error!r}")
        logger.opt(exception=cleanup_error).error(
            "termination committed but shell cleanup also failed after publication failure",
            agent_id=agent_id,
            cutoff=cutoff,
        )
    if publication_error is not None:
        raise publication_error
