"""Event contract registry (R2-C) — see contract.py."""

from shared.events.contract import (
    EVENTS,
    LLM_ERROR_FAMILY,
    OPS_BUCKET_S,
    OPS_GRID_ORIGIN,
    TIER_BY_EVENT,
    Category,
    EventSpec,
    EventTier,
    category_for_kind,
    family_events,
    payload_keys,
    telemetry_events,
    tier_for,
)

__all__ = [
    "EVENTS",
    "LLM_ERROR_FAMILY",
    "OPS_BUCKET_S",
    "OPS_GRID_ORIGIN",
    "TIER_BY_EVENT",
    "Category",
    "EventSpec",
    "EventTier",
    "category_for_kind",
    "family_events",
    "payload_keys",
    "telemetry_events",
    "tier_for",
]
