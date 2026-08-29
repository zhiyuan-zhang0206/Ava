"""Small scheduler adapter for the disabled-by-default retention planner."""

from __future__ import annotations

from dataclasses import dataclass

from services.pitr.retention_inventory import GCSRetentionInventoryReader
from services.pitr.retention_planner import DryRunResult, write_dry_run_plan
from shared.config.physical_backup import PhysicalBackupSettings
from shared.health_schema import DEGRADED, OK, component
from shared.paths import ava_home


@dataclass
class RetentionDryRunState:
    enabled: bool = False
    plan: DryRunResult | None = None


def health_component(state: RetentionDryRunState) -> dict[str, object]:
    plan = state.plan
    record = component(
        "pitr_retention_dry_run",
        DEGRADED if plan is not None and plan.blocked else OK,
        progress=(
            "disabled"
            if not state.enabled
            else "blocked"
            if plan is not None and plan.blocked
            else "dry-run"
        ),
        detail="retention evidence is incomplete; eligibility forced empty"
        if plan is not None and plan.blocked
        else None,
        gate_readiness=False,
    )
    record["delete_enabled"] = False
    record["plan_digest"] = plan.digest if plan is not None else None
    record["retained_objects"] = plan.retained_objects if plan is not None else 0
    record["eligible_objects"] = plan.eligible_objects if plan is not None else 0
    record["retained_bytes"] = plan.retained_bytes if plan is not None else 0
    record["eligible_bytes"] = plan.eligible_bytes if plan is not None else 0
    return record


def refresh(config: PhysicalBackupSettings) -> DryRunResult:
    credentials = config.pitr_restore_gcs_credentials_file
    if credentials is None:
        raise RuntimeError("validated retention viewer credential is missing")
    return write_dry_run_plan(
        ava_home() / "physical-backup",
        retain_chains=config.pitr_retained_weekly_chains,
        inventory_reader=GCSRetentionInventoryReader(
            project=config.pitr_gcs_project,
            bucket=config.pitr_gcs_bucket,
            prefix=config.pitr_gcs_prefix,
            credentials_file=credentials,
        ),
    )
