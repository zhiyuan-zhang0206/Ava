"""Inbound message delivery watchdog events."""

from __future__ import annotations

from typing import TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class DeliveryStalled(TypedDict):
    """`delivery_stalled` payload — services/wake/delivery_watchdog/daemon.py."""

    inbound_id: int
    age_s: float


class DeliveryPoisoned(TypedDict):
    """`delivery_poisoned` payload — delivery watchdog dispatch guard."""

    inbound_id: int
    dispatch_count: int
    age_s: float


EVENTS: dict[str, EventSpec] = {
    "delivery_stalled": telemetry_event(
        "delivery_stalled",
        "delivery backlog",
        payload=DeliveryStalled,
        tier="anomaly",
        site="services/wake/delivery_watchdog/daemon.py:_alert_stalled",
        persist=True,
    ),
    "delivery_poisoned": telemetry_event(
        "delivery_poisoned",
        "delivery backlog — permanently-failing inbound poisoned (dispatch cap reached)",
        payload=DeliveryPoisoned,
        tier="anomaly",
        site="services/wake/delivery_watchdog/dispatch_guard.py:_alert_poisoned",
    ),
}
