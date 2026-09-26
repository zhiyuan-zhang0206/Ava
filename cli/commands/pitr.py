"""PITR inspection and rollback-snapshot archive commands."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from services.pitr.base_manifest import CandidateManifest
from services.pitr.retention_planner import inspect_dry_run_plan
from services.pitr.rollback_snapshot_archive import (
    RollbackSnapshotArchive,
    archive_rollback_snapshot,
    drop_rollback_snapshot_table,
    export_rollback_snapshot_table,
    restore_rollback_snapshot_table,
    retire_rollback_snapshot,
    verify_rollback_snapshot,
)
from services.pitr.store_factory import get_store_group
from shared.config import settings
from shared.db import direct_db_url
from shared.paths import ava_home
from shared.pg_tools import pg_tool


def cmd_pitr_retention_status() -> int:
    from services.pitr.retention_gate import (
        CarrierState,
        display_armed,
        iso_timestamp,
        journal_line,
        journal_tail,
        plan_line,
        read_daemon_record,
        read_plan,
    )

    carriers = CarrierState.read()
    plan = read_plan()
    daemon = read_daemon_record()
    print(f"retention gate @ {ava_home()}")
    print(
        f"  carriers: armed={display_armed(armed=carriers.armed)} "
        f"approved_digest={carriers.approved_digest or 'unset'}"
    )
    if plan is None:
        print("  plan:     none on disk yet")
    else:
        print(f"  plan:     {plan_line(plan)}")
        print(f"            updated {iso_timestamp(plan.mtime)}")
    if daemon is None:
        print("  daemon:   unreachable - showing file state only")
    else:
        totals = daemon.get("delete_totals")
        lines = [
            f"  daemon:   delete_state={daemon.get('delete_state')} "
            f"armed_at={iso_timestamp(daemon.get('armed_at'))}",
            f"            stable_ticks={daemon.get('digest_stable_ticks')}",
        ]
        if isinstance(totals, dict):
            totals_record = cast("dict[str, object]", totals)
            lines.append(
                f"            totals: ticks={totals_record.get('ticks')} "
                f"deleted={totals_record.get('deleted')} "
                f"absent={totals_record.get('absent')} failed={totals_record.get('failed')}"
            )
        detail = daemon.get("delete_error") or daemon.get("detail")
        if detail:
            lines.append(f"            detail: {detail}")
        print("\n".join(lines))
    for record in journal_tail():
        print(f"  journal:  {journal_line(record)}")
    return 0


def cmd_pitr_retention_arm(*, digest: str, confirm: bool) -> int:
    from services.pitr.retention_gate import (
        CarrierState,
        append_gate_record,
        plan_line,
        read_plan,
        write_arm_carriers,
    )

    plan = read_plan()
    if plan is None:
        print(
            "no dry-run plan on disk yet; the scheduler writes one each tick - "
            "wait for the next tick, then re-run",
            file=sys.stderr,
        )
        return 1
    print(f"plan: {plan_line(plan)}")
    if plan.blocked_reasons:
        print(
            "refusing to arm: the plan carries blockers; eligibility is forced empty",
            file=sys.stderr,
        )
        return 1
    if plan.digest != digest:
        print(
            f"refusing to arm: --digest {digest} does not match the plan on disk "
            f"({plan.digest}); read `retention status` and approve the current digest",
            file=sys.stderr,
        )
        return 1
    if not confirm:
        print("preview only: re-run with --confirm to write the arm carriers")
        return 0
    before = CarrierState.read()
    append_gate_record("arm", phase="intent", plan_digest=digest, before=before)
    write_arm_carriers(digest)
    after = CarrierState.read()
    append_gate_record("arm", phase="applied", plan_digest=digest, after=after)
    print(
        "armed: the scheduler re-reads the carriers on its next tick; deletion still "
        "requires the digest to hold for consecutive ticks before the first execution"
    )
    return 0


def cmd_pitr_retention_disable(*, confirm: bool) -> int:
    from services.pitr.retention_gate import (
        CarrierState,
        append_gate_record,
        clear_arm_carriers,
        display_armed,
    )

    before = CarrierState.read()
    if not confirm:
        print(
            f"preview only: would clear armed={display_armed(armed=before.armed)} "
            f"approved_digest={before.approved_digest or 'unset'}; re-run with --confirm"
        )
        return 0
    append_gate_record("disable", phase="intent", plan_digest=before.approved_digest, before=before)
    clear_arm_carriers()
    after = CarrierState.read()
    append_gate_record("disable", phase="applied", plan_digest=None, after=after)
    print("disabled: carriers cleared; the scheduler returns to dry-run on its next tick")
    return 0


def cmd_pitr_retention_run_once(*, confirm: bool) -> int:
    """One deletion pass on the operator's explicit command (design 3.5)."""
    from services.pitr.retention_gate import CarrierState

    carriers = CarrierState.read()
    if not carriers.armed or carriers.approved_digest is None:
        print(
            "retention deletion is not armed; run `ava pitr retention arm` first",
            file=sys.stderr,
        )
        return 1
    if not confirm:
        print(
            "preview only: would recompute the plan, re-compare the approved digest, "
            "and run one bounded pass through the executor; re-run with --confirm"
        )
        return 0
    from services.pitr import retention_scheduler

    try:
        summary = retention_scheduler.run_operator_once(settings.physical_backup)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    deleted = summary.deleted + summary.sidecars_deleted + summary.orphans_deleted
    absent = summary.absent + summary.sidecars_absent + summary.orphans_absent
    failed = (
        summary.failed + summary.verify_failed + summary.sidecars_failed + summary.orphans_failed
    )
    print(
        f"operator pass complete: deleted={deleted} absent={absent} "
        f"failed={failed} skipped={summary.skipped}"
    )
    if summary.refused_reason:
        print(f"refused: {summary.refused_reason}", file=sys.stderr)
        return 1
    return 0


def cmd_pitr_retention_inspect() -> int:
    from services.pitr.retention_gate import CarrierState

    try:
        plan = inspect_dry_run_plan(ava_home() / "physical-backup")
    except FileNotFoundError:
        print(
            "no dry-run plan on disk yet; the scheduler writes one each tick - "
            "wait for the next tick, then re-run",
            file=sys.stderr,
        )
        return 1
    print(
        json.dumps(
            {
                "plan_digest": plan.digest(),
                "blocked_reasons": plan.blocked_reasons,
                "protected_chain_ids": plan.protected_chain_ids,
                "unprotected_chain_ids": plan.unprotected_chain_ids,
                "oldest_retained_chain_id": plan.oldest_retained_chain_id,
                "ack_high_water": plan.ack_high_water,
                "retained_objects": len(plan.retained),
                "eligible_objects": len(plan.eligible),
                "retained_bytes": plan.retained_bytes,
                "eligible_bytes": plan.eligible_bytes,
                "logical_retained_objects": sum(
                    1 for item in plan.retained if item.object.kind == "logical"
                ),
                "logical_eligible_objects": sum(
                    1 for item in plan.eligible if item.object.kind == "logical"
                ),
                "weak_evidence_objects": len(plan.weak_evidence),
                "delete_enabled": CarrierState.read().armed is True,
            },
            sort_keys=True,
        )
    )
    return 0 if not plan.blocked_reasons else 2


def cmd_pitr_snapshot_archive(table: str) -> int:
    """Export and publish one finite migration rollback snapshot."""
    return _run_snapshot_action(
        "archive",
        lambda: archive_rollback_snapshot(
            table,
            ava_home=ava_home(),
            key=_pitr_backup_key(),
            key_id=settings.physical_backup.pitr_backup_key_id,
            export_table=export_rollback_snapshot_table,
            store=get_store_group().object_store(),
        ),
    )


def cmd_pitr_snapshot_verify(table: str) -> int:
    """Restore one archived rollback snapshot into disposable PostgreSQL."""
    return _run_snapshot_action(
        "verify",
        lambda: verify_rollback_snapshot(
            table,
            ava_home=ava_home(),
            key=_pitr_backup_key(),
            reader=get_store_group().generation_pinned_object_reader(),
            restore_drill=restore_rollback_snapshot_table,
        ),
    )


def cmd_pitr_snapshot_retire(table: str) -> int:
    """Drop one rollback snapshot after its exact archived generation is proven."""
    return _run_snapshot_action(
        "retire",
        lambda: retire_rollback_snapshot(
            table,
            ava_home=ava_home(),
            drop_table=drop_rollback_snapshot_table,
        ),
    )


def _run_snapshot_action(action: str, run: Callable[[], RollbackSnapshotArchive]) -> int:
    """Report snapshot command failures without exposing a Python traceback."""
    try:
        record = run()
    except Exception as exc:
        print(f"pitr snapshot {action} failed: {exc}", file=sys.stderr)
        return 1
    print(record.to_json())
    return 0


def _pitr_backup_key() -> bytes:
    path = settings.physical_backup.pitr_backup_key_file
    if path is None:
        raise RuntimeError("PITR backup key file is not configured")
    return path.read_bytes()


def cmd_pitr_drill(
    *,
    chain: str | None,
    candidate: str | None,
    target_lsn: str,
    target_wall: str,
    scratch: str,
    timeout_seconds: int,
) -> int:
    """Restore one protected chain to an operator target in isolation.

    The scratch tree is kept as evidence on every outcome; the drill never
    publishes and never writes to the live cluster. The controller launches a
    restricted viewer-only interpreter and accepts evidence after group closure.
    """
    import asyncio

    from services.pitr.base_operation_runtime import (
        RestoreWorkerInput,
        live_data_directory,
        restore_key_path,
        restore_store_args,
        run_drill_input,
    )
    from services.pitr.restore_drill import parse_target_wall
    from services.pitr.restore_proof import RestoreSpaceBudget

    scratch_path = Path(scratch)
    evidence_path = scratch_path / "drill-evidence.json"
    try:
        candidate_manifest = _resolve_drill_candidate(chain, candidate)
        config = settings.physical_backup
        root = ava_home() / "physical-backup"
        inputs = RestoreWorkerInput(
            candidate_manifest.to_json(),
            root,
            root / "ack",
            restore_key_path(config),
            config.pitr_store_backend,
            restore_store_args(config),
            RestoreSpaceBudget(0, 0, 0),
            direct_db_url(),
            live_data_directory(),
            pg_tool("pg_ctl"),
            pg_tool("pg_verifybackup"),
        )
        evidence = asyncio.run(
            run_drill_input(
                inputs,
                scratch=scratch_path,
                target_lsn=target_lsn,
                target_wall=parse_target_wall(target_wall).isoformat(),
                timeout_seconds=timeout_seconds,
            )
        )
    except Exception as exc:
        print(f"pitr drill failed: {exc} (evidence: {evidence_path})", file=sys.stderr)
        return 1
    print(json.dumps({"evidence": str(evidence_path), **evidence}, sort_keys=True))
    return 0


def _resolve_drill_candidate(chain: str | None, candidate: str | None) -> CandidateManifest:
    if (chain is None) == (candidate is None):
        raise ValueError("pass exactly one of --chain or --candidate")
    if candidate is not None:
        return CandidateManifest.from_json(Path(candidate).read_text())
    path = ava_home() / "physical-backup" / "base-manifests" / f"{chain}.candidate.json"
    if not path.is_file():
        raise ValueError(f"no candidate manifest for chain {chain!r} at {path}")
    return CandidateManifest.from_json(path.read_text())


def cmd_pitr_multipart_list(*, prefix: str, credentials_file: str | None) -> int:
    """List every incomplete multipart upload (orphan shard) under ``prefix``."""
    from services.pitr.object_store import ObjectStoreError
    from services.pitr.oss_multipart import OSSMultipartUploads

    try:
        endpoint, bucket, path = _multipart_target(credentials_file)
        surface = OSSMultipartUploads(endpoint=endpoint, bucket=bucket, credentials_file=path)
        rows = surface.inventory(prefix=prefix)
    except (ObjectStoreError, RuntimeError, ValueError) as exc:
        print(f"pitr multipart list failed: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("no incomplete multipart uploads")
        return 0
    now = int(datetime.now(tz=UTC).timestamp())
    for row in rows:
        print(
            f"{row.key}  upload_id={row.upload_id}  parts={row.part_count} "
            f"bytes={row.size_bytes}  initiated={_iso_utc(row.initiated)} "
            f"age={_age_text(now - row.initiated)}"
        )
    return 0


def cmd_pitr_multipart_abort(
    *, key: str, upload_id: str, credentials_file: str | None, confirm: bool
) -> int:
    """Abort exactly one incomplete multipart upload; --confirm is the switch."""
    from services.pitr.object_store import ObjectStoreError
    from services.pitr.oss_multipart import AbortOutcome, OSSMultipartUploads

    try:
        endpoint, bucket, path = _multipart_target(credentials_file)
        surface = OSSMultipartUploads(endpoint=endpoint, bucket=bucket, credentials_file=path)
        target = surface.find(key=key, upload_id=upload_id)
    except (ObjectStoreError, RuntimeError, ValueError) as exc:
        print(f"pitr multipart abort failed: {exc}", file=sys.stderr)
        return 1
    if target is None:
        print(
            f"not found: no incomplete multipart upload key={key} upload_id={upload_id} "
            "(already completed or aborted?); nothing was changed",
            file=sys.stderr,
        )
        return 1
    now = int(datetime.now(tz=UTC).timestamp())
    print(
        f"target: key={target.key} upload_id={target.upload_id} parts={target.part_count} "
        f"bytes={target.size_bytes} initiated={_iso_utc(target.initiated)} "
        f"age={_age_text(now - target.initiated)}"
    )
    if not confirm:
        print("preview only: re-run with --confirm to abort this upload")
        return 0
    try:
        outcome = surface.abort(key=key, upload_id=upload_id)
    except (ObjectStoreError, RuntimeError, ValueError) as exc:
        print(f"pitr multipart abort failed: {exc}", file=sys.stderr)
        return 1
    if outcome is not AbortOutcome.ABORTED:
        print(
            f"nothing to abort: key={key} upload_id={upload_id} vanished mid-confirm "
            "(completed or aborted concurrently)",
            file=sys.stderr,
        )
        return 1
    print(f"aborted: key={key} upload_id={upload_id}")
    return 0


def _multipart_target(credentials_file: str | None) -> tuple[str, str, str]:
    """(endpoint, bucket, credential file) for the OSS multipart surface."""
    config = settings.physical_backup
    configured = config.pitr_oss_credentials_file
    path = credentials_file or (str(configured) if configured is not None else None)
    if path is None:
        raise RuntimeError(
            "no OSS credential file: pass --credentials-file or set AVA_PITR_OSS_CREDENTIALS_FILE"
        )
    if not config.pitr_oss_endpoint or not config.pitr_oss_bucket:
        raise RuntimeError(
            "OSS endpoint and bucket are not configured "
            "(AVA_PITR_OSS_ENDPOINT / AVA_PITR_OSS_BUCKET)"
        )
    return config.pitr_oss_endpoint, config.pitr_oss_bucket, path


def _iso_utc(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_text(seconds: int) -> str:
    seconds = max(seconds, 0)
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"
