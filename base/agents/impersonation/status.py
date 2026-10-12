"""Impersonation lease status vocabulary and status-only row validation."""

from collections.abc import Mapping
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


# Wire version of the event log a lease records into its own entries (protocol v2).
LOG_PROTOCOL_VERSION = 2

# Relay heartbeat cadence, the staleness window after which a wake alerts, and how long a recorded
# failure suppresses a repeat alert.
RELAY_HEARTBEAT_SECONDS = 10.0
RELAY_HEARTBEAT_STALE_SECONDS = 45.0
RELAY_FAILURE_STAMP_SECONDS = 600


def is_log_native(lease: Mapping[str, Any]) -> bool:
    """Whether this lease records its events in its own entries (protocol v2)."""
    return (
        lease["automatic"] is True
        and lease["event_delivery_protocol_version"] == LOG_PROTOCOL_VERSION
    )


def pending_reason(lease: Mapping[str, Any]) -> str | None:
    """Classify a handoff's current delivery uncertainty without inventing state."""
    if lease["events_completed_at"] is not None:
        return None
    if lease["automatic"] is not True:
        return "manual"
    if lease["event_delivery_protocol_version"] != LOG_PROTOCOL_VERSION:
        return "legacy"
    stored = lease["event_delivery_pending_reason"]
    if stored is not None:
        return str(stored)
    return "awaiting_session_end" if lease["ended_at"] is None else "awaiting_participant_seal"
