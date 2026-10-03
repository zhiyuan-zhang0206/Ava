"""Inbound message delivery watchdog and outbox events."""

from __future__ import annotations

from typing import TypedDict

from base.events.vocabulary import EventSpec, telemetry_event


class DeliveryStalled(TypedDict):
    """`delivery_stalled` payload — services/delivery_watchdog/daemon.py."""

    inbound_id: int
    age_s: float


class DeliveryPoisoned(TypedDict):
    """`delivery_poisoned` payload — delivery watchdog dispatch guard."""

    inbound_id: int
    dispatch_count: int
    age_s: float


class DeliveryWakeSuppressed(TypedDict):
    """`delivery_wake_suppressed` payload — delivery watchdog resurrection guard."""

    consecutive_failures: int
    suppress_seconds: float
    suppress_count: int
    reason: str


class DeliveryRecoveryDecision(TypedDict):
    """`delivery_recovery_decision` payload — services/delivery_watchdog/daemon.py.

    One decision the delivery watchdog obtained for a stalled chat whose owner
    is a crash-marked idling corpse (task #3618): `decision` is the home
    runner's verdict ('harvested' / 'already_terminated' / 'refused') or a
    local transport outcome ('unreachable' / 'error'); `reason` carries the
    fail-closed cause of a refusal, else None."""

    inbound_id: int
    decision: str
    reason: str | None


class DeliveryOutboxFlushed(TypedDict):
    """`delivery_outbox_flushed` payload — base/agents/messages/delivery_outbox.py flusher."""

    inbound_id: int
    attempts: int
    flush_attempts: int
    age_s: float
    origin_agent_id: int | None


class DeliveryOutboxAbandoned(TypedDict):
    """`delivery_outbox_abandoned` payload — base/agents/messages/delivery_outbox.py flusher.

    `reason` stays the stable code readers match on; `detail` carries the
    readable failure text when the abandonment had one (gate refusal, key
    conflict, last transport error).
    """

    reason: str
    detail: str | None
    attempts: int
    flush_attempts: int
    age_s: float
    origin_agent_id: int | None


EVENTS: dict[str, EventSpec] = {
    "delivery_stalled": telemetry_event(
        "delivery_stalled",
        "delivery backlog",
        payload=DeliveryStalled,
        tier="anomaly",
        site="services/delivery_watchdog/daemon.py:_alert_stalled",
        persist=True,
    ),
    "delivery_poisoned": telemetry_event(
        "delivery_poisoned",
        "delivery backlog — permanently-failing inbound poisoned (dispatch cap reached)",
        payload=DeliveryPoisoned,
        tier="anomaly",
        site="services/delivery_watchdog/dispatch_guard.py:_alert_poisoned",
    ),
    "delivery_wake_suppressed": telemetry_event(
        "delivery_wake_suppressed",
        "automatic delivery wakes suppressed after repeated resurrection failures",
        payload=DeliveryWakeSuppressed,
        tier="anomaly",
        site="services/delivery_watchdog/resurrect_guard.py:_alert_wake_suppressed",
    ),
    "delivery_recovery_decision": telemetry_event(
        "delivery_recovery_decision",
        "stalled crash-marked recovery decision (harvest / refusal)",
        payload=DeliveryRecoveryDecision,
        tier="anomaly",
        site="services/delivery_watchdog/stall_recovery.py:_request_harvest",
    ),
    "delivery_outbox_flushed": telemetry_event(
        "delivery_outbox_flushed",
        "delivery backlog — a deferred-send record was redelivered (task #3757)",
        payload=DeliveryOutboxFlushed,
        site="base/agents/messages/delivery_outbox.py:flush",
    ),
    "delivery_outbox_abandoned": telemetry_event(
        "delivery_outbox_abandoned",
        "delivery backlog — a deferred-send record abandoned at its budget or on a "
        "permanent failure (task #3757)",
        payload=DeliveryOutboxAbandoned,
        tier="anomaly",
        site="base/agents/messages/delivery_outbox.py:_abandon",
    ),
}
