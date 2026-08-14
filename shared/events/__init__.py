"""Event contract registry (R2-C) — see contract.py."""

from shared.events.contract import (
    EVENTS,
    LLM_ERROR_FAMILY,
    OPS_BUCKET_S,
    OPS_GRID_ORIGIN,
    RETENTION_BY_CATEGORY,
    Category,
    EventSpec,
    category_for_kind,
    family_events,
    payload_keys,
    retention_days,
    telemetry_events,
)

__all__ = [
    "EVENTS",
    "LLM_ERROR_FAMILY",
    "OPS_BUCKET_S",
    "OPS_GRID_ORIGIN",
    "RETENTION_BY_CATEGORY",
    "Category",
    "EventSpec",
    "category_for_kind",
    "family_events",
    "payload_keys",
    "retention_days",
    "telemetry_events",
]
