"""Scheduler adapter for the disabled-by-default retention machinery.

Two halves share one state object: the dry-run planner refresh (delete-free)
and the arm-gated deletion state machine (task #2150 P1c). The deletion design
keeps the delete path off by default (v0.3 section 3): the explicit retention
commands write the arm carrier and the operator-approved plan digest into the
unit `.env`; this daemon re-reads both from the file on every tick, so a flip
takes effect on the next tick without a restart.

The machine mirrors the design states ``disabled`` (planner off) -> ``dry-run``
-> ``armed`` -> ``deleting``. The arm carrier alone deletes nothing:

- the freshly recomputed plan must be unblocked and its digest must equal the
  operator-approved digest;
- the digest must hold for ``_DIGEST_STABILITY_TICKS`` consecutive ticks before
  the first execution;
- the first execution tick of a process is a double-run: the plan is recomputed
  and its digest re-compared immediately before anything is deleted;
- every executed tick is journalled and bounded by the executor's limits; any
  doubt keeps the state out of ``deleting`` for that tick.

Only memory holds the live state (a restart resets the stability counter and
the totals — the conservative direction); the journal under
``$AVA_HOME/physical-backup/retention-journal/`` is the durable audit trail.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from services.pitr.retention_executor import RetentionExecutionSummary, execute_retention_plan
from services.pitr.retention_journal import RetentionJournal
from services.pitr.retention_planner import (
    DryRunResult,
    inspect_dry_run_plan,
    write_dry_run_plan,
)
from services.pitr.retention_policy import LogicalRetention
from services.pitr.store_factory import get_store_group
from shared import telemetry
from shared.config.physical_backup import PhysicalBackupSettings
from shared.health_schema import DEGRADED, OK, component
from shared.paths import ava_home

_log = logging.getLogger("services.pitr.retention_scheduler")

_ARM_FLAG_ALIAS = "AVA_PITR_RETENTION_DELETE_ARMED"
_APPROVED_DIGEST_ALIAS = "AVA_PITR_RETENTION_DELETE_APPROVED_DIGEST"
# Consecutive identical unblocked digests required after arm before the first
# execution (design section 3.3, "K consecutive stable digests").
_DIGEST_STABILITY_TICKS = 2
# Baidu's delete becomes visible only when the listing drops the row; the exact
# settling window is calibrated by the scratch real-run (design section 2.5) --
# until then the probe is a conservative bounded poll: a row still visible after
# the bound is recorded as verify-failed, never retried within the tick.
_BAIDU_VERIFY_ATTEMPTS = 5
_BAIDU_VERIFY_INTERVAL_S = 2.0
_STALE_AFTER_S = 2 * 3600


@dataclass
class RetentionDeleteTotals:
    """Cumulative deletions since process start (memory only, per the design)."""

    ticks: int = 0
    deleted: int = 0
    absent: int = 0
    failed: int = 0


@dataclass
class RetentionDeleteState:
    """The arm-gated deletion half of the retention state machine."""

    armed: bool = False
    approved_digest: str | None = None
    status: str = "disabled"
    armed_at: float | None = None
    stable_count: int = 0
    last_stable_digest: str | None = None
    executed_this_process: bool = False
    last_delete_tick: float | None = None
    last_summary: RetentionExecutionSummary | None = None
    last_error: str | None = None
    totals: RetentionDeleteTotals = field(default_factory=RetentionDeleteTotals)


@dataclass
class RetentionDryRunState:
    enabled: bool = False
    plan: DryRunResult | None = None
    last_attempt: float | None = None
    last_success: float | None = None
    last_error: str | None = None
    delete: RetentionDeleteState = field(default_factory=RetentionDeleteState)


def _logical_retention() -> LogicalRetention:
    """The logical namespace's retention window, mirroring the local pool.

    The depths come from the live ``services`` config (``backup_keep`` /
    ``ACTIVATION_KEEP``) and the in-flight activation pin from
    ``services.backup`` itself, so the off-site mirror cannot drift from the
    local prune.
    """
    from services.backup import ACTIVATION_KEEP, active_activation_snapshot_name
    from shared.config import settings

    return LogicalRetention(
        keep_dailies=settings.services.backup_keep,
        keep_pre_updates=1,
        keep_activations=ACTIVATION_KEEP,
        legacy_tz=ZoneInfo(settings.general.timezone),
        active_pin_name=active_activation_snapshot_name(),
    )


def _read_carrier(alias: str) -> str | None:
    """Fresh raw value of one carrier key from the unit `.env`, or None."""
    from shared import runtime_config

    return runtime_config.read_env_aliases().get(alias)


def _live_delete_armed(*, boot_value: bool) -> bool:
    """The arm carrier, re-read from the unit `.env` on every tick.

    The settings singleton is frozen at process start, but the design promises
    "arm takes effect on the next tick" (v0.3 section 3.3) -- so the key the
    commands write must be read from the file. A missing key keeps the
    boot-time value.
    """
    raw = _read_carrier(_ARM_FLAG_ALIAS)
    if raw is None:
        return boot_value
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _live_approved_digest(*, boot_value: str | None) -> str | None:
    raw = _read_carrier(_APPROVED_DIGEST_ALIAS)
    if raw is None:
        return boot_value
    return raw.strip() or None


def _reset_stability(state: RetentionDeleteState) -> None:
    state.stable_count = 0
    state.last_stable_digest = None
    state.armed_at = None


def _hold_reason(plan: DryRunResult | None, approved_digest: str | None) -> str | None:
    if plan is None:
        return "no fresh retention plan is available for this tick"
    if plan.blocked:
        return "the retention plan carries blockers; deletion is held"
    if approved_digest is None:
        return "no operator-approved plan digest is set; deletion is held"
    if plan.digest != approved_digest:
        return "the plan digest differs from the operator-approved digest; deletion is held"
    return None


def _require_plan(state: RetentionDryRunState) -> DryRunResult:
    plan = state.plan
    if plan is None:
        raise RuntimeError("retention plan vanished between checks")
    return plan


def delete_tick(state: RetentionDryRunState, config: PhysicalBackupSettings) -> None:
    """Advance the deletion state machine one scheduler tick.

    Called right after the dry-run refresh, so ``state.plan`` is this tick's
    fresh plan. Never raises: a failure is recorded on the delete state and
    the scheduler loop continues (deletion always fails closed).
    """
    delete = state.delete
    try:
        if not state.enabled:
            delete.status = "disabled"
            delete.armed = False
            delete.approved_digest = None
            _reset_stability(delete)
            return
        delete.armed = _live_delete_armed(boot_value=config.pitr_retention_delete_armed)
        delete.approved_digest = _live_approved_digest(
            boot_value=config.pitr_retention_delete_approved_digest
        )
        if not delete.armed:
            delete.status = "dry-run"
            delete.last_error = None
            _reset_stability(delete)
            return
        held = _hold_reason(state.plan, delete.approved_digest)
        if held is not None:
            delete.status = "dry-run"
            delete.last_error = held
            _reset_stability(delete)
            return
        plan = _require_plan(state)
        if plan.digest == delete.last_stable_digest:
            delete.stable_count += 1
        else:
            delete.last_stable_digest = plan.digest
            delete.stable_count = 1
        if delete.stable_count < _DIGEST_STABILITY_TICKS:
            delete.status = "dry-run"
            delete.last_error = None
            return
        if delete.armed_at is None:
            delete.armed_at = time.time()
        delete.status = "armed"
        _execute_tick(state, config)
    except Exception as exc:
        delete.last_error = str(exc)
        delete.status = "armed" if delete.armed else "dry-run"
        _log.exception("PITR retention delete tick failed; deletion held")


def _execute_tick(state: RetentionDryRunState, config: PhysicalBackupSettings) -> None:
    delete = state.delete
    delete.status = "deleting"
    try:
        summary = _run_delete_pass(state, config)
        delete.last_summary = summary
        delete.last_delete_tick = time.time()
        delete.executed_this_process = summary.refused_reason is None
        delete.last_error = summary.refused_reason
        totals = delete.totals
        totals.ticks += 1
        totals.deleted += summary.deleted + summary.sidecars_deleted + summary.orphans_deleted
        totals.absent += summary.absent + summary.sidecars_absent + summary.orphans_absent
        totals.failed += (
            summary.failed
            + summary.verify_failed
            + summary.sidecars_failed
            + summary.orphans_failed
        )
        telemetry.emit(
            "telemetry",
            "pitr_retention_delete_tick",
            attributes={
                "backend": config.pitr_store_backend,
                "deleted": totals.deleted,
                "absent": totals.absent,
                "failed": totals.failed,
            },
        )
    except Exception as exc:
        delete.last_error = str(exc)
        _log.exception("PITR retention deletion pass failed; retrying next tick")
    finally:
        delete.status = "armed"


def _run_delete_pass(
    state: RetentionDryRunState, config: PhysicalBackupSettings
) -> RetentionExecutionSummary:
    delete = state.delete
    root = ava_home() / "physical-backup"
    plan_result = state.plan
    if plan_result is None:
        raise RuntimeError("no fresh retention plan is available for this tick")
    if not delete.executed_this_process:
        # The design's first-execution double-run: recompute the plan and
        # compare digests immediately before this process's first deletion.
        recheck = write_dry_run_plan(
            root,
            retain_chains=config.pitr_retained_weekly_chains,
            inventory_reader=get_store_group().retention_inventory_reader(),
            logical_reader=get_store_group().logical_retention_inventory_reader(),
            logical_retention=_logical_retention(),
        )
        if recheck.digest != delete.approved_digest:
            raise RuntimeError(
                "plan digest changed between the decision and the first-execution recheck"
            )
        plan_result = recheck
    expected_digest = delete.approved_digest
    if expected_digest is None:
        raise RuntimeError("no operator-approved plan digest is set")
    return execute_retention_plan(
        inspect_dry_run_plan(root),
        expected_digest=expected_digest,
        delete_store=get_store_group().retention_delete_store(),
        verify_absent=_build_verify_absent(config),
        remote_total_bytes=plan_result.remote_bytes,
        journal=RetentionJournal(root / "retention-journal"),
    )


def _build_verify_absent(config: PhysicalBackupSettings) -> Callable[[str], bool]:
    """The executor's absence re-observation, one probe per backend.

    OSS/GCS/COS answer from a direct stat (head/None). Baidu's listing lags
    its delete, so the probe is a bounded poll (see the module constants).
    """
    store = get_store_group().viewer_object_store()
    if config.pitr_store_backend != "baidu":
        return lambda object_name: store.stat(object_name) is None

    def poll(object_name: str) -> bool:
        for attempt in range(_BAIDU_VERIFY_ATTEMPTS):
            if store.stat(object_name) is None:
                return True
            if attempt + 1 < _BAIDU_VERIFY_ATTEMPTS:
                time.sleep(_BAIDU_VERIFY_INTERVAL_S)
        return False

    return poll


def run_operator_once(config: PhysicalBackupSettings) -> RetentionExecutionSummary:
    """Execute one retention pass on the operator's explicit command (3.5).

    The operator-present first run: the live carriers must say armed with an
    approved digest, the plan is recomputed and its digest compared right
    before execution (the same double-run guard as a daemon first tick), and
    the pass goes through the same bounded executor as the daemon inline path.
    Raises ValueError on any doubt, before deleting anything.
    """
    from services.pitr.retention_gate import CarrierState, append_gate_record

    root = ava_home() / "physical-backup"
    journal = RetentionJournal(root / "retention-journal")
    carriers = CarrierState.read()
    digest = carriers.approved_digest
    if not carriers.armed or digest is None:
        raise ValueError("retention deletion is not armed; run `ava pitr retention arm` first")
    append_gate_record("run-once", phase="intent", plan_digest=digest, before=carriers)
    recheck = write_dry_run_plan(
        root,
        retain_chains=config.pitr_retained_weekly_chains,
        inventory_reader=get_store_group().retention_inventory_reader(),
        logical_reader=get_store_group().logical_retention_inventory_reader(),
        logical_retention=_logical_retention(),
    )
    if recheck.blocked or recheck.digest != digest:
        reason = (
            "the fresh plan carries blockers; eligibility is forced empty"
            if recheck.blocked
            else "the fresh plan digest differs from the approved digest"
        )
        append_gate_record(
            "run-once", phase="refused", plan_digest=recheck.digest, extra={"reason": reason}
        )
        raise ValueError(f"refusing the operator pass: {reason}")
    summary = execute_retention_plan(
        inspect_dry_run_plan(root),
        expected_digest=digest,
        delete_store=get_store_group().retention_delete_store(),
        verify_absent=_build_verify_absent(config),
        remote_total_bytes=recheck.remote_bytes,
        journal=journal,
    )
    append_gate_record(
        "run-once",
        phase="result",
        plan_digest=digest,
        extra={
            "refused_reason": summary.refused_reason,
            "deleted": summary.deleted + summary.sidecars_deleted + summary.orphans_deleted,
            "absent": summary.absent + summary.sidecars_absent + summary.orphans_absent,
            "failed": (
                summary.failed
                + summary.verify_failed
                + summary.sidecars_failed
                + summary.orphans_failed
            ),
        },
    )
    return summary


def health_component(state: RetentionDryRunState) -> dict[str, object]:
    plan = state.plan
    delete = state.delete
    stale = state.last_success is not None and time.time() - state.last_success > _STALE_AFTER_S
    unavailable = state.enabled and (
        plan is None or state.last_error is not None or state.last_success is None or stale
    )
    current = state.enabled and not unavailable
    delete_degraded = delete.status in {"armed", "deleting"} and delete.last_error is not None
    record = component(
        "pitr_retention_dry_run",
        DEGRADED if unavailable or (plan is not None and plan.blocked) or delete_degraded else OK,
        progress=(
            "disabled"
            if not state.enabled
            else "stale"
            if stale
            else delete.status
            if delete.status in {"armed", "deleting"}
            else "blocked"
            if unavailable or (plan is not None and plan.blocked)
            else "dry-run"
        ),
        detail=(
            state.last_error
            or delete.last_error
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
    record["delete_enabled"] = delete.status in {"armed", "deleting"}
    record["delete_state"] = delete.status
    record["armed_at"] = delete.armed_at
    record["last_delete_tick"] = delete.last_delete_tick
    record["delete_error"] = delete.last_error
    record["digest_stable_ticks"] = delete.stable_count
    record["delete_totals"] = {
        "ticks": delete.totals.ticks,
        "deleted": delete.totals.deleted,
        "absent": delete.totals.absent,
        "failed": delete.totals.failed,
    }
    record["current"] = current
    record["last_attempt"] = state.last_attempt
    record["plan_digest"] = plan.digest if plan is not None and current else None
    record["retained_objects"] = plan.retained_objects if plan is not None and current else 0
    record["eligible_objects"] = plan.eligible_objects if plan is not None and current else 0
    record["retained_bytes"] = plan.retained_bytes if plan is not None and current else 0
    record["eligible_bytes"] = plan.eligible_bytes if plan is not None and current else 0
    record["logical_object_count"] = (
        plan.logical_object_count if plan is not None and current else 0
    )
    record["logical_bytes"] = plan.logical_bytes if plan is not None and current else 0
    record["logical_eligible_objects"] = (
        plan.logical_eligible_objects if plan is not None and current else 0
    )
    record["weak_evidence_objects"] = (
        plan.weak_evidence_objects if plan is not None and current else 0
    )
    return record


def refresh(config: PhysicalBackupSettings) -> DryRunResult:
    credentials = config.pitr_restore_gcs_credentials_file
    if credentials is None:
        raise RuntimeError("validated retention viewer credential is missing")
    result = write_dry_run_plan(
        ava_home() / "physical-backup",
        retain_chains=config.pitr_retained_weekly_chains,
        inventory_reader=get_store_group().retention_inventory_reader(),
        logical_reader=get_store_group().logical_retention_inventory_reader(),
        logical_retention=_logical_retention(),
    )
    telemetry.emit(
        "telemetry",
        "pitr_remote_inventory",
        attributes={
            "backend": config.pitr_store_backend,
            "object_count": result.remote_object_count,
            "bytes": result.remote_bytes,
            "logical_object_count": result.logical_object_count,
            "logical_bytes": result.logical_bytes,
        },
    )
    return result
