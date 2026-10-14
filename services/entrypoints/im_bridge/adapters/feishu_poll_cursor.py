"""Durable position of Feishu's inbound polling.

The platform keeps no offset for the polling path, so the adapter keeps one:
the newest handled message id plus its create time, saved after every advance
(`im_bridge_cursors`, `cursor_store.py`). After a restart the first round of
each saved chat resumes from the saved position instead of re-seeding at the
newest message; it reads one page (20), so a gap holding more messages loses
the older ones. The replay window (the agent side's own stale bound for chat
inbounds) keeps a long outage from flooding the agent; the gateway dedups each
message on `idempotency_key`, so a message re-read after a crash between
delivery and save does not become a second inbound.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import requests

from services.entrypoints.im_bridge.types import RetryableTransportError, raise_http_failure


class FeishuListRejected(RuntimeError):  # noqa: N818 — reads as the outcome, like its log message "Feishu list rejected"
    """The list call reached the platform and was rejected at the API level.

    A ``code != 0`` response (quota, permission, invalid container) is not a
    transport failure and not an HTTP status rejection, but it still fails the
    poll round: the loop keeps it off the crash path and logs it.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


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


def pending_after(
    items: list[Any],
    cursor_id: str | None,
    cursor_ms: int | None,
    *,
    replay: bool,
    replay_window_s: float,
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
    cutoff = now_ms() - int(replay_window_s * 1000)
    fresh: list[Any] = []
    stale: list[Any] = []
    for item in gap:
        created = create_ms(item)
        (stale if created is not None and created < cutoff else fresh).append(item)
    return fresh, stale


async def list_chat(rest_client: Any, chat_id: str) -> list[Any]:
    """The chat's newest page of messages (20), ascending; failed list calls raise."""

    from lark_oapi.api.im.v1 import ListMessageRequest

    request = (
        ListMessageRequest.builder()
        .container_id_type("chat")
        .container_id(chat_id)
        .page_size(20)
        .sort_type("ByCreateTimeDesc")
        .build()
    )
    try:
        response = await asyncio.to_thread(rest_client.im.v1.message.list, request)
    except (requests.exceptions.SSLError, requests.exceptions.ProxyError):
        raise
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise RetryableTransportError(f"Feishu list request failed: {type(exc).__name__}") from None
    if response.raw is not None and response.raw.status_code != 200:
        raise_http_failure(response.raw.status_code, "Feishu list request failed")
    if response.code != 0:
        raise FeishuListRejected(response.code, f"Feishu list rejected: code={response.code}")
    data = response.data
    items: list[Any] = list((data.items or []) if data is not None else [])
    items.reverse()
    return items
