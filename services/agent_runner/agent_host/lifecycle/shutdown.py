"""Release a joined host's clients and settled ownership without losing primary errors."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from base.agents.context.clients import ClientSet
from base.native_process.turn_identity import HostedServiceResources

__all__ = ["close_host_resources"]


async def _close_joined_clients_and_owner(
    clients: ClientSet,
    release_owner: Callable[[], Awaitable[None]],
    *,
    release_timeout: float,
) -> None:
    """Stop constructed clients, then attempt the existing bounded ownership release.

    A writer failure cannot skip lease release or replace its own original error.
    Unfinished ordinary delivery retains its ClientSet owner until hard exit.
    """
    primary: BaseException | None = None
    try:
        await asyncio.to_thread(clients.close)
    except BaseException as exc:
        primary = exc
    try:
        try:
            async with asyncio.timeout(release_timeout):
                await release_owner()
        except TimeoutError as exc:
            raise TimeoutError(
                f"hosted ownership release did not land within {release_timeout:g}s; "
                "its leases expire by TTL"
            ) from exc
    except BaseException as secondary:
        if primary is None:
            primary = secondary
        else:
            primary.add_note(f"Hosted ownership release also failed: {secondary!r}")
    if primary is not None:
        raise primary


async def close_host_resources(
    service: HostedServiceResources,
    clients: ClientSet,
    release_owner: Callable[[], Awaitable[None]],
    *,
    clear_runtimes: Callable[[], None],
    resource_deadline: float | None,
    release_timeout: float,
) -> None:
    """Join native resources before releasing clients and settled ownership.

    An unfinished resource join keeps clients and pools live. Once joined, every
    cleanup runs and the first original error retains later cleanup as notes.
    """
    primary: BaseException | None = None
    try:
        await service.aclose(deadline=resource_deadline)
    except BaseException as exc:
        primary = exc
    if service.joined:
        clear_runtimes()
        try:
            await _close_joined_clients_and_owner(
                clients, release_owner, release_timeout=release_timeout
            )
        except BaseException as secondary:
            if primary is None:
                primary = secondary
            else:
                primary.add_note(f"Host client cleanup also failed: {secondary!r}")
    if primary is not None:
        raise primary
