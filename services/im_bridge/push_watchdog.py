"""Weixin push-failure watchdog (Task #829).

iLink's context_token (carried by each user message) expires after a short,
undetermined window (measured 11min..2h+). When it does, sendmessage fails
silently with ret=-2 while getUpdates keeps working — the user can still
message the bot but never receives replies. The only refresh is the user's
own message, so:

- after enough consecutive failures, alert the user through the OTHER
  channels (Telegram / Feishu) telling them to message the bot;
- after a later send succeeds (a fresh user message brought a new token),
  hint "recovered" on the next inbound reply.

The old "24h window" reminder assumed a fixed 24h expiry and was retired.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from services.im_bridge import copy
from shared.config import settings

_log = logging.getLogger("services.im_bridge.core.push_watchdog")

PUSH_FAILURE_THRESHOLD = 2  # consecutive failures before alerting
PUSH_ALERT_COOLDOWN_SECONDS = 1800  # seconds between alerts
PUSH_RECOVERED_HINT_SECONDS = 60  # seconds after first success to hint

_sleep = asyncio.sleep  # module-local seam: tests patch this name, not asyncio.sleep


def retry_backoff_seconds() -> float:
    """Bounded jitter backoff before a retry: base + U(0, jitter).

    The measured failure mode is a connection-establishment window of
    ~0.65 s (probe, task #4252): an immediate retry re-enters the same
    window, while the default 1.0 s base plus 0-2.0 s of jitter walks
    past it."""

    base = settings.services.im_push_retry_backoff_seconds
    jitter = settings.services.im_push_retry_jitter_seconds
    return base + random.uniform(0.0, jitter)  # noqa: S311 — spread, not secrecy


async def retry_once_after_backoff(attempt: Callable[[], Awaitable[None]]) -> None:
    """Sleep a bounded jitter backoff, then run one retry attempt.

    Exactly one retry — never a loop. A failure of the retry propagates to
    the caller, which owns the logging and the outcome. Shared by the push
    path (``send_with_retry``) and the ops-alert fan-out
    (``IMBridgeCore.notify_user``)."""

    await _sleep(retry_backoff_seconds())
    await attempt()


async def send_with_retry(core: Any, channel: str, chat_id: str, reply: Any, adapter: Any) -> None:
    """Send once, retry once after a bounded jitter backoff; on the retry
    failure also run the weixin push-failure watchdog (alert the user
    through other channels)."""

    async def attempt() -> None:
        await adapter.send(chat_id, reply.text, buttons=reply.buttons, markdown=reply.markdown)

    try:
        await attempt()
    except Exception:
        _log.exception("send failed channel=%s chat=%s", channel, chat_id)
        try:
            await retry_once_after_backoff(attempt)
        except Exception:
            _log.exception("send retry failed channel=%s chat=%s", channel, chat_id)
            await alert_push_failure(core, channel, adapter)


async def alert_push_failure(core: Any, channel: str, adapter: Any) -> None:
    """Alert the user through other channels when a channel's outbound
    pushes keep failing. Inbound always works, so the alert must go out
    over a different channel than the failing one."""

    failures = getattr(adapter, "push_failures", 0)
    failed_at = getattr(adapter, "push_failed_at", None)
    if channel != "weixin" or failures < PUSH_FAILURE_THRESHOLD or failed_at is None:
        return
    alerted_at = getattr(adapter, "_push_alerted_at", None)
    if alerted_at is not None and time.time() - alerted_at < PUSH_ALERT_COOLDOWN_SECONDS:
        return
    text = copy.PUSH_FAILURE_ALERT.format(channel=channel, failures=failures)
    sent_anywhere = False
    for other, other_adapter in core.adapters.items():
        if other == channel:
            continue
        try:
            await other_adapter.send_to_owner(text)
            sent_anywhere = True
        except Exception as exc:  # one broken channel must not stop the rest
            _log.warning("push-failure alert via %s failed: %r", other, exc)
    if sent_anywhere:
        adapter._push_alerted_at = time.time()  # type: ignore[attr-defined]


async def hint_recovered(core: Any, msg: Any) -> None:
    """Right after a weixin push failure, the user's own message carries a
    fresh context_token and the first send succeeds — tell them the link is
    back."""

    if msg.channel != "weixin":
        return
    adapter = core.adapters.get("weixin")
    if adapter is None:
        return
    recovered_at = getattr(adapter, "push_recovered_at", None)
    if recovered_at is None:
        return
    if time.time() - recovered_at > PUSH_RECOVERED_HINT_SECONDS:
        return
    from services.im_bridge.types import Reply

    try:
        await core._send(
            msg.channel,
            msg.chat_id,
            Reply(copy.PUSH_RECOVERED_HINT.format(channel=msg.channel)),
        )
    except Exception:
        _log.exception("weixin recovered-hint send failed")
