"""Inbound message delivery watchdog events."""

from __future__ import annotations

from typing import TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class DeliveryStalled(TypedDict):
    """`delivery_stalled` payload — services/wake/delivery_watchdog/daemon.py."""

    inbound_id: int
    age_s: float


class InboundConsumedUnknown(TypedDict):
    inbound_id: int
    kind: str
    age_s: float
    content_preview: str


EVENTS: dict[str, EventSpec] = {
    "inbound_consumed_unknown": telemetry_event(
        "inbound_consumed_unknown",
        "a claimed inbound was consumed without knowing whether its work ran "
        "(host crash / aborted turn); the row is marked in its payload and is "
        "never redelivered — this event is the only record of what was lost",
        payload=InboundConsumedUnknown,
        tier="anomaly",
        site="agent/db/__init__.py:finalize_claimed_inbounds",
        persist=True,
    ),
    "delivery_stalled": telemetry_event(
        "delivery_stalled",
        "delivery backlog",
        payload=DeliveryStalled,
        tier="anomaly",
        site="services/wake/delivery_watchdog/daemon.py:_alert_stalled",
        persist=True,
    ),
}
