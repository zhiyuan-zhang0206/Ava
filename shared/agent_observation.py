"""Observation evidence, independent of lifecycle or execution progress."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel

# Existing machine-probe cadence and consecutive-failure contract.
LIVENESS_PASS_INTERVAL_S = 60.0
MACHINE_OFFLINE_AFTER_FAILURES = 2


class AgentObservation(BaseModel):
    """Machine reachability is not proof of runtime ownership.

    This compatibility slice has no generation/owner binding. A fresh lease
    alone, or a PID, must not invent one. UI uses absolute deadlines.
    """

    machine_probe_at: datetime | None = None
    machine_probe_valid_until: datetime | None = None
    runtime_lease_expires_at: datetime | None = None
    runtime_owner: Literal["unknown"] = "unknown"


def observation(
    machine_probe_at: datetime | None, lease_expires_at: datetime | None
) -> AgentObservation:
    return AgentObservation(
        machine_probe_at=machine_probe_at,
        machine_probe_valid_until=(
            machine_probe_at
            + timedelta(seconds=LIVENESS_PASS_INTERVAL_S * MACHINE_OFFLINE_AFTER_FAILURES)
            if machine_probe_at is not None
            else None
        ),
        runtime_lease_expires_at=lease_expires_at,
    )


PROBE_FRESH_FOR = timedelta(seconds=LIVENESS_PASS_INTERVAL_S * MACHINE_OFFLINE_AFTER_FAILURES)
ADMISSION_FRESH_FOR = timedelta(minutes=5)


class AdmissionOutcome(StrEnum):
    ADMITTED = "admitted"
    MAINTENANCE_HOLD = "maintenance_hold"
    PUBLICATION_DEFERRED = "publication_deferred"
    RESOURCE_FENCE = "resource_fence"
    ADMISSION_GUARD_REFUSED = "admission_guard_refused"


class AvailabilityReason(StrEnum):
    UNKNOWN = "unknown"
    HOST_UNAVAILABLE = "host_unavailable"
    AWAITING_ADMISSION = "awaiting_admission"
    ADMISSION_REFUSED = "admission_refused"
    ADMITTED = "admitted"


class AgentAvailability(BaseModel):
    """A time-bounded observation, never proof of first-message completion."""

    reason: AvailabilityReason
    observed_at: datetime
    evidence_at: datetime | None = None
    admission_outcome: AdmissionOutcome | None = None


def _fresh(stamp: datetime | None, at: datetime, max_age: timedelta) -> bool:
    return stamp is not None and timedelta(0) <= at - stamp <= max_age


def availability(
    *,
    status: str,
    host_online: bool | None,
    probe_at: datetime | None,
    admission_outcome: str | None,
    admission_at: datetime | None,
    now: datetime | None = None,
) -> AgentAvailability:
    """Host health wins over older agent history; stale evidence becomes unknown.

    `host_online` is the existing pidfile verdict carried by status_probe, not
    proof that a turn can pass publication/resource admission. An admission
    observation is useful only while both it and the host probe are fresh.
    """
    at = now or datetime.now(UTC)
    if (admission_outcome is None) != (admission_at is None):
        raise ValueError("admission outcome and timestamp must be paired")
    outcome = AdmissionOutcome(admission_outcome) if admission_outcome is not None else None
    latest_at = max(
        (stamp for stamp in (probe_at, admission_at) if stamp is not None), default=None
    )
    if status == "terminated" or host_online is None or not _fresh(probe_at, at, PROBE_FRESH_FOR):
        return AgentAvailability(
            reason=AvailabilityReason.UNKNOWN, observed_at=at, evidence_at=latest_at
        )
    if not host_online:
        return AgentAvailability(
            reason=AvailabilityReason.HOST_UNAVAILABLE, observed_at=at, evidence_at=probe_at
        )
    if outcome is None:
        return AgentAvailability(
            reason=AvailabilityReason.AWAITING_ADMISSION, observed_at=at, evidence_at=probe_at
        )
    if not _fresh(admission_at, at, ADMISSION_FRESH_FOR):
        return AgentAvailability(
            reason=AvailabilityReason.UNKNOWN, observed_at=at, evidence_at=latest_at
        )
    return AgentAvailability(
        reason=(
            AvailabilityReason.ADMITTED
            if outcome == AdmissionOutcome.ADMITTED
            else AvailabilityReason.ADMISSION_REFUSED
        ),
        observed_at=at,
        evidence_at=admission_at,
        admission_outcome=outcome,
    )
