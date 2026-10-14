"""Per-leg push-health sampling for the leg-staleness alert (task #4935).

The #4932 incident (2026-10-03) stranded a channel's pushes for about five
hours with no line anywhere; the adopted alignment keeps detection in code and
alerting in Grafana. This module emits one structured ``im_push_health`` event
per configured channel per interval -- seconds since the channel's last
delivered push (``last_ok_age_s``) and pushes still waiting for delivery
(``pending``). It decides nothing: the Grafana rule owns thresholds, dedup and
routing.

The data source is the durable outbox (``im_bridge_outbound_intents``), the
ledger every accepted content push (timeline, notice, alert-group) lands in:
``pending`` counts rows still waiting for a delivery attempt (``queued`` and
``sending``); the age is measured from the newest ``sent`` row. A channel with
no delivered push on record reports ``PUSH_HEALTH_NEVER_SENT``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from base.deploy.maintenance import admission
from base.log import logger
from services.entrypoints.im_bridge.config import ImBridgeConfig
from services.entrypoints.im_bridge.outbound.store import IMOutboxStore

#: ``last_ok_age_s`` when the channel has no delivered push on record (fresh
#: deployment, or a leg that never delivered). Negative -- it can never satisfy
#: a ``last_ok_age_s > threshold`` condition, and the alert rule pairs it with
#: an explicit never-delivered branch.
PUSH_HEALTH_NEVER_SENT = -1.0


async def emit_push_health_once(store: IMOutboxStore, config: ImBridgeConfig) -> None:
    """Read the outbox ledger once and emit one event per configured channel."""

    snapshot = await asyncio.to_thread(
        store.push_health_snapshot, tuple(config.im_push_health_channels)
    )
    now = datetime.now(UTC)
    for channel in config.im_push_health_channels:
        pending, last_ok = snapshot.get(channel, (0, None))
        if last_ok is None:
            age = PUSH_HEALTH_NEVER_SENT
        else:
            age = max(0.0, (now - last_ok).total_seconds())
        logger.info(
            "push_health channel={channel} last_ok_age_s={last_ok_age_s} pending={pending}",
            event="im_push_health",
            channel=channel,
            last_ok_age_s=round(age, 1),
            pending=pending,
        )


async def push_health_loop(
    store: IMOutboxStore,
    config: ImBridgeConfig,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Sample every configured leg each ``im_push_health_interval_seconds``.

    Rounds are skipped while a maintenance hold quiesces the unit, matching
    the other resident loops: the hold already stops the send path, so a
    sample taken then would read a stall the hold itself explains.

    ``sleep`` is the loop's scheduling boundary: production uses
    ``asyncio.sleep``; tests pass a fake that ends the loop.
    """

    while True:
        if not admission.quiesced():
            await emit_push_health_once(store, config)
        await sleep(config.im_push_health_interval_seconds)
