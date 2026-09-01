"""Small scheduler adapter for the disabled-by-default retention planner."""

from __future__ import annotations

import time
from dataclasses import dataclass

from services.pitr.retention_planner import DryRunResult, write_dry_run_plan
from services.pitr.store_factory import get_store_group
from shared import telemetry
from shared.config.physical_backup import PhysicalBackupSettings
from shared.health_schema import DEGRADED, OK, component
from shared.paths import ava_home


@dataclass
class RetentionDryRunState:
    enabled: bool = False
    plan: DryRunResult | None = None
    last_attempt: float | None = None
    last_success: float | None = None
    last_error: str | None = None


_STALE_AFTER_S = 2 * 3600


def health_component(state: RetentionDryRunState) -> dict[str, object]:
    plan = state.plan
    stale = state.last_success is not None and time.time() - state.last_success > _STALE_AFTER_S
    unavailable = state.enabled and (
        plan is None or state.last_error is not None or state.last_success is None or stale
    )
    current = state.enabled and not unavailable
    record = component(
        "pitr_retention_dry_run",
        DEGRADED if unavailable or (plan is not None and plan.blocked) else OK,
        progress=(
            "disabled"
            if not state.enabled
            else "stale"
            if stale
            else "blocked"
            if unavailable or (plan is not None and plan.blocked)
            else "dry-run"
        ),
        detail=(
            state.last_error
            or ("retention dry-run plan is stale" if stale else None)
            or ("no fresh retention dry-run plan exists" if unavailable else None)
            or (
                "retention evidence is incomplete; eligibility forced empty"
                if plan is not None and plan.blocked
                else None
            )
        ),
        last_success=state.last_success,
        now=time.time() if state.last_success is not None else None,
        gate_readiness=False,
    )
    record["delete_enabled"] = False
    record["current"] = current
    record["last_attempt"] = state.last_attempt
    record["plan_digest"] = plan.digest if plan is not None and current else None
    record["retained_objects"] = plan.retained_objects if plan is not None and current else 0
    record["eligible_objects"] = plan.eligible_objects if plan is not None and current else 0
    record["retained_bytes"] = plan.retained_bytes if plan is not None and current else 0
    record["eligible_bytes"] = plan.eligible_bytes if plan is not None and current else 0
    return record


def refresh(config: PhysicalBackupSettings) -> DryRunResult:
    credentials = config.pitr_restore_gcs_credentials_file
    if credentials is None:
        raise RuntimeError("validated retention viewer credential is missing")
    result = write_dry_run_plan(
        ava_home() / "physical-backup",
        retain_chains=config.pitr_retained_weekly_chains,
        inventory_reader=get_store_group().retention_inventory_reader(),
    )
    telemetry.emit(
        "telemetry",
        "pitr_remote_inventory",
        attributes={
            "backend": config.pitr_store_backend,
            "object_count": result.remote_object_count,
            "bytes": result.remote_bytes,
        },
    )
    return result
