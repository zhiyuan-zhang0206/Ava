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

import logging
import time
from typing import Any

from services.im_bridge import copy

_log = logging.getLogger("services.im_bridge.core.push_watchdog")

PUSH_FAILURE_THRESHOLD = 2  # consecutive failures before alerting
PUSH_ALERT_COOLDOWN_SECONDS = 1800  # seconds between alerts
PUSH_RECOVERED_HINT_SECONDS = 60  # seconds after first success to hint


async def send_with_retry(core: Any, channel: str, chat_id: str, reply: Any, adapter: Any) -> None:
    """Send once, retry once; on the retry failure also run the weixin
    push-failure watchdog (alert the user through other channels)."""

    try:
        await adapter.send(chat_id, reply.text, buttons=reply.buttons, markdown=reply.markdown)
    except Exception:
        _log.exception("send failed channel=%s chat=%s", channel, chat_id)
        try:
            await adapter.send(chat_id, reply.text, buttons=reply.buttons, markdown=reply.markdown)
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
    from services.im_bridge.core import Reply  # local import avoids a cycle

    try:
        await core._send(
            msg.channel,
            msg.chat_id,
            Reply(copy.PUSH_RECOVERED_HINT.format(channel=msg.channel)),
        )
    except Exception:
        _log.exception("weixin recovered-hint send failed")
