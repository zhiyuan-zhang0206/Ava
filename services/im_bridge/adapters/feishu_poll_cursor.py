"""Durable position of Feishu's inbound polling.

The platform keeps no offset for the polling path, so the adapter keeps one:
the newest handled message id plus its create time, saved after every advance
(`im_bridge_cursors`, `cursor_store.py`). After a restart the first round of
each saved chat pages back through the gap instead of re-seeding at the newest
message. The replay window (the agent side's own stale bound for chat
inbounds) keeps a long outage from flooding the agent; the gateway dedups each
message on `idempotency_key`, so a message re-read after a crash between
delivery and save does not become a second inbound.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from base.log import logger


def idempotency_key(message_id: str | None) -> str | None:
    """The gateway's keyed-delivery dedup key for one Feishu message (the WS
    and polling paths and every replay resolve to the same inbound)."""

    return f"feishu:{message_id}" if message_id else None


def now_ms() -> int:
    return int(time.time() * 1000)


def create_ms(item: Any) -> int | None:
    """A listed message's create time in epoch ms; None when absent/garbled."""

    try:
        return int(getattr(item, "create_time", ""))
    except (TypeError, ValueError):
        return None


def replay_window_s() -> float:
    """How old an unhandled message may be and still be replayed: the agent
    side dead-letters chat inbounds older than this rather than re-deliver
    them, so the bridge never feeds an agent what it would refuse as ancient."""

    from base.config import settings

    return settings.daemon.delivery_watchdog_stale_claimed_threshold_seconds


def anchor(items: list[Any]) -> tuple[str | None, int]:
    """Where a never-polled chat is anchored: its newest message (nothing is
    processed). Anchors on the newest item that carries an id: an id-less
    newest item (defensive — the API guarantees ids) must not leave the chat
    without a cursor, which would replay the whole window. An empty window
    still yields a time position, so the first message afterwards is replayed
    after a restart rather than mistaken for history."""

    newest = next((item for item in reversed(items) if item.message_id), None)
    if newest is None:
        return None, now_ms()
    return newest.message_id, create_ms(newest) or now_ms()


def reaches_cursor(
    pages_newest_first: list[Any], cursor_id: str | None, cursor_ms: int | None
) -> bool:
    """Whether the pages listed so far already hold everything the replay can
    want: the cursor id itself, a message older than the cursor's time, or one
    older than the replay window."""

    cutoff = now_ms() - int(replay_window_s() * 1000)
    bound = max(cutoff, cursor_ms) if cursor_ms is not None else cutoff
    for item in pages_newest_first:
        created = create_ms(item)
        if (cursor_id is not None and item.message_id == cursor_id) or (
            created is not None and created < bound
        ):
            return True
    return False


def pending_after(
    items: list[Any], cursor_id: str | None, cursor_ms: int | None, *, replay: bool
) -> tuple[list[Any], list[Any]]:
    """The messages to process after the cursor, as (to process, too old).

    Position: after the cursor id when it is listed. Replaying after a restart
    and the id not listed (rotated out, or a time-only cursor): by create time
    (`>=`, because the platform's clock is millisecond-grained and a duplicate
    is deduped by the gateway while a skipped message is lost). A replay also
    drops what is older than the replay window; a live round never does."""

    ids = [item.message_id for item in items]
    if cursor_id is not None and cursor_id in ids:
        gap = items[ids.index(cursor_id) + 1 :]
    elif replay and cursor_ms is not None:
        gap = [item for item in items if (create_ms(item) or 0) >= cursor_ms]
    else:
        gap = items
    if not replay:
        return gap, []
    cutoff = now_ms() - int(replay_window_s() * 1000)
    fresh: list[Any] = []
    stale: list[Any] = []
    for item in gap:
        created = create_ms(item)
        (stale if created is not None and created < cutoff else fresh).append(item)
    return fresh, stale


async def list_chat(
    rest_client: Any,
    chat_id: str,
    *,
    deep: bool,
    cursor_id: str | None = None,
    cursor_ms: int | None = None,
) -> list[Any] | None:
    """The chat's newest messages, ascending; None when a list call failed.

    One page (20) normally. `deep` (the replay after a restart) pages back
    until the cursor, or the replay window, is reached: an outage that held
    more than a page of messages must not lose the older ones. A failed page
    fails the whole round, which is retried from scratch."""

    from lark_oapi.api.im.v1 import ListMessageRequest

    items: list[Any] = []
    token: str | None = None
    while True:
        builder = (
            ListMessageRequest.builder()
            .container_id_type("chat")
            .container_id(chat_id)
            .page_size(20)
            .sort_type("ByCreateTimeDesc")
        )
        if token:
            builder = builder.page_token(token)
        response = await asyncio.to_thread(rest_client.im.v1.message.list, builder.build())
        if response.code != 0:
            logger.warning(
                "FeishuAdapter: poll list failed chat={} code={} msg={}",
                chat_id,
                response.code,
                getattr(response, "msg", ""),
            )
            return None
        data = response.data
        items += list((data.items or []) if data is not None else [])
        token = getattr(data, "page_token", None)
        if not (
            deep
            and getattr(data, "has_more", False)
            and token
            and not reaches_cursor(items, cursor_id, cursor_ms)
        ):
            break
    items.reverse()
    return items
