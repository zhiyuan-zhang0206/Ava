"""Observation evidence, independent of lifecycle or execution progress."""

from datetime import datetime, timedelta
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
