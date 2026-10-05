"""Impersonation lease status vocabulary and status-only row validation."""

from enum import StrEnum
from typing import Any, Literal, TypedDict, cast, get_args


class ImpersonationStatus(StrEnum):
    """Persisted cooperative lease lifecycle; distinct from native agent status."""

    REQUESTED = "requested"
    ACCEPTED = "accepted"
    ACTIVE = "active"
    RELEASED = "released"
    REJECTED = "rejected"
    EXPIRED = "expired"


OpenImpersonationStatus = Literal[
    ImpersonationStatus.REQUESTED, ImpersonationStatus.ACCEPTED, ImpersonationStatus.ACTIVE
]
OPEN = cast(tuple[OpenImpersonationStatus, ...], tuple(get_args(OpenImpersonationStatus)))


class LeaseRecord(TypedDict):
    """Lifecycle row: status is parsed; other database fields retain their existing types."""

    status: ImpersonationStatus
    session_id: Any
    next_entry: Any
    handoff_path: Any
    events_completed_at: Any
    event_delivery_protocol_version: Any
    event_delivery_pending_reason: Any
    event_admission_closed_at: Any
    ended_at: Any
    accepted_generation: Any
    accepted_owner: Any
    ack_window_seconds: Any
    agent_id: Any
    applied_version: Any
    automatic: Any
    consent_version: Any
    delta_version: Any
    expires_at: Any
    handoff_applied_at: Any
    id: Any
    machine: Any
    max_delivery_attempts: Any
    relay_provider: Any
    relay_generation: Any
    relay_degraded_reason: Any
    relay_token_hash: Any
    process_metadata: Any
    source: Any
    summary_inbound_id: Any
    summary: Any
    rejection_reason: Any
    ttl_seconds: Any


def parse_lease(row: dict[str, Any]) -> LeaseRecord:
    """Validate only status as a raw SQL row enters lease lifecycle logic."""
    row["status"] = ImpersonationStatus(row["status"])
    return cast(LeaseRecord, row)
