"""Weixin push-failure watchdog (Task #829).

iLink's context_token (carried by each user message) expires after a short,
undetermined window (measured 11min..2h+). When it does, sendmessage fails
silently with ret=-2 while getUpdates keeps working — the user can still
message the bot but never receives replies. The only refresh is the user's
own message, so:

- a failed retry emits the ``im_push_failed`` event carrying the adapter's
  consecutive-failure count; the alert rule over the event stream tells the
  user (through the ops fan-out) to message the bot;
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

from base.log import logger
from services.im_bridge import copy
from services.im_bridge.config import ImBridgeConfig

_log = logging.getLogger("services.im_bridge.core.push_watchdog")

PUSH_RECOVERED_HINT_SECONDS = 60  # seconds after first success to hint

_sleep = asyncio.sleep  # module-local seam: tests patch this name, not asyncio.sleep


def retry_backoff_seconds(config: ImBridgeConfig) -> float:
    """Bounded jitter backoff before a retry: base + U(0, jitter).

    The measured failure mode is a connection-establishment window of
    ~0.65 s (probe, task #4252): an immediate retry re-enters the same
    window, while the default 1.0 s base plus 0-2.0 s of jitter walks
    past it."""

    base = config.im_push_retry_backoff_seconds
    jitter = config.im_push_retry_jitter_seconds
    return base + random.uniform(0.0, jitter)  # noqa: S311 — spread, not secrecy


async def retry_once_after_backoff(
    attempt: Callable[[], Awaitable[None]], config: ImBridgeConfig
) -> None:
    """Sleep a bounded jitter backoff, then run one retry attempt.

    Exactly one retry — never a loop. A failure of the retry propagates to
    the caller, which owns the logging and the outcome. Shared by the push
    path (``send_with_retry``) and the ops-alert fan-out
    (``IMBridgeCore.notify_user``)."""

    await _sleep(retry_backoff_seconds(config))
    await attempt()


async def send_with_retry(core: Any, channel: str, chat_id: str, reply: Any, adapter: Any) -> None:
    """Send once, retry once after a bounded jitter backoff; on the retry
    failure emit the ``im_push_failed`` event."""

    async def attempt() -> None:
        await adapter.send(chat_id, reply.text, buttons=reply.buttons, markdown=reply.markdown)

    try:
        await attempt()
    except Exception as exc:
        # The first failure is the transient norm (the measured ~0.65s connect
        # window, a flaky link): WARNING with the one-line cause, no traceback.
        # The single retry decides whether it was real — its failure logs at
        # ERROR and escalates (2026-10-03 triage, E3).
        _log.warning("send failed channel=%s chat=%s: %r — retrying once", channel, chat_id, exc)
        try:
            await retry_once_after_backoff(attempt, core.config)
        except Exception:
            _log.exception("send retry failed channel=%s chat=%s", channel, chat_id)
            logger.warning(
                "push failed after retry: {channel}",
                event="im_push_failed",
                channel=channel,
                failures=getattr(adapter, "push_failures", 0),
            )


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
