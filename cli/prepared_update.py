"""Explicit operator dispatch for a retained image, before settings or source.

Image and expected inventory are not a bootable rollback or all-writer closure.
After read-only validation this entry creates/joins the prepared operation:
the verified coordinator acquires the sole deployment operation and every unit
binds the exact request, records its preflight, and waits at the all-unit
barrier. No post-barrier cutover stage (writer closure, migration, selector,
service start/readback, finalization) is implemented or authorized here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import stat
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

import psycopg
from pydantic import AwareDatetime, Field, ValidationError, model_validator

from services.agent_ops.bootstrap import ObserverProjection
from shared.managed_writer_barrier import (
    Digest,
    EvidenceModel,
    ManagedWriterBarrierError,
    ManagedWriterCollection,
)
from shared.managed_writer_publication import (
    NormalStartPlan,
    PreparedDispatch,
    PreparedUnitPreflight,
    PublishedUnit,
)
from shared.runtime_interpreter import WHEEL_RUNTIME, runtime_venv
from shared.runtime_publication_input import PreparationReceipt
from shared.runtime_release import (
    ReleaseRejectedError,
    VerifiedRelease,
    file_sha256,
    verify_release,
)
from shared.verified_file import regular_bytes


def _now() -> datetime:
    """Provide a narrow clock seam for deadline-bound dispatch polling."""
    return datetime.now(UTC)


class PreparedOperatorUnit(EvidenceModel):
    unit: PublishedUnit
    recovery: PublishedUnit
    recovery_schema_digest: Digest


class PreparedOperatorPlan(EvidenceModel):
    """Identical immutable bytes on every unit; no participant-local lease."""

    version: Literal[1]
    request_id: UUID
    target_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    coordinator: PublishedUnit
    units: tuple[PreparedOperatorUnit, ...] = Field(min_length=1)
    valid_until: AwareDatetime
    normal: NormalStartPlan
    recovery_collection: str | None = None

    @model_validator(mode="after")
    def exact_roster(self) -> Self:
        units = tuple(item.unit for item in self.units)
        if units != tuple(item.unit for item in self.normal.units) or self.coordinator not in units:
            raise ValueError("operator request and normal plan require the same complete roster")
        for item in self.units:
            if (item.recovery.machine, item.recovery.home) != (
                item.unit.machine,
                item.unit.home,
            ) or item.recovery.artifact_digest == item.unit.artifact_digest:
                raise ValueError("recovery image must be distinct and belong to the same unit")
        return self


@dataclass(frozen=True)
class PreparedOperatorInput:
    path: Path
    digest: str
    request: PreparedOperatorPlan
    local: PreparedOperatorUnit
    image: VerifiedRelease
    recovery: VerifiedRelease
    receipt: PreparationReceipt


def _private_plan(path: Path, home: Path) -> bytes:
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or path.parent != home / "run"
        or stat.S_IMODE(path.stat().st_mode) != 0o600
        or path.stat().st_uid != os.getuid()
    ):
        raise ReleaseRejectedError("prepared entry requires a canonical private unit plan")
    return regular_bytes(path)


def _image(unit: PublishedUnit, schema_digest: str) -> tuple[VerifiedRelease, PreparationReceipt]:
    home = Path(unit.home)
    if home.resolve(strict=True) != home:
        raise ReleaseRejectedError("prepared unit home is not canonical")
    if regular_bytes(home / "machine_name").decode().strip() != unit.machine:
        raise ReleaseRejectedError("prepared unit machine differs from installed identity")
    image = verify_release(
        home / "releases",
        unit.artifact_digest,
        manifest_digest=unit.manifest_digest,
        platform_tag=platform.platform(),
        schema_digest=schema_digest,
    )
    raw = regular_bytes(home / "run" / f"release-inventory-{unit.inventory_digest}.json")
    if hashlib.sha256(raw).hexdigest() != unit.inventory_digest:
        raise ReleaseRejectedError("prepared complete inventory receipt digest differs")
    receipt = PreparationReceipt.model_validate_json(raw)
    expected = receipt.expected
    if (expected.machine, expected.home, expected.artifact_digest, expected.manifest_digest) != (
        unit.machine,
        unit.home,
        image.digest,
        image.manifest_digest,
    ) or receipt.inventory_digest != expected.unit().inventory_digest:
        raise ReleaseRejectedError("prepared inventory belongs to another unit/image")
    return image, receipt


def prepare_operator_input(path: Path) -> PreparedOperatorInput:
    """Read the actual imported image before any mutable settings/source import."""
    if not WHEEL_RUNTIME or sys.platform not in {"linux", "darwin"}:
        raise ReleaseRejectedError("prepared entry requires a supported retained POSIX runtime")
    prefix = runtime_venv()
    root = prefix.parent
    home = root.parent.parent
    if prefix.name != "venv" or root.parent.name != "releases":
        raise ReleaseRejectedError("operator CLI is not inside a unit release")
    raw = _private_plan(path, home)
    request = PreparedOperatorPlan.model_validate_json(raw)
    machine = regular_bytes(home / "machine_name").decode().strip()
    matches = [
        item
        for item in request.units
        if (item.unit.machine, item.unit.home) == (machine, str(home))
    ]
    if len(matches) != 1 or matches[0].unit.artifact_digest != root.name:
        raise ReleaseRejectedError("operator/recovery image and installed unit differ")
    local = matches[0]
    if request.valid_until <= datetime.now(UTC):
        raise ReleaseRejectedError("prepared entry deadline expired; no operation was started")
    # Observe the installed baseline rather than trusting a manifest self-claim.
    package_root = Path(__file__).resolve().parent.parent
    if not package_root.is_relative_to(prefix):
        raise ReleaseRejectedError("prepared CLI module escapes the loaded interpreter")
    if file_sha256(package_root / "db/schema.sql") != request.normal.schema_digest:
        raise ReleaseRejectedError("prepared schema differs from loaded SQL baseline")
    image, receipt = _image(local.unit, request.normal.schema_digest)
    recovery, _ = _image(local.recovery, local.recovery_schema_digest)
    if image.root != root:
        raise ReleaseRejectedError("normal plan does not belong to the loaded candidate")
    if _private_plan(path, home) != raw:
        raise ReleaseRejectedError("prepared operator input changed during verification")
    return PreparedOperatorInput(
        path, hashlib.sha256(raw).hexdigest(), request, local, image, recovery, receipt
    )


def _connect_timeout(valid_until: AwareDatetime) -> int:
    """Refuse a database dial that cannot finish before the immutable deadline."""
    remaining = int((valid_until - _now()).total_seconds())
    if remaining <= 0:
        raise ReleaseRejectedError("prepared dispatch deadline expired; no services were stopped")
    return min(5, remaining)


def _prepared_evidence_digest(prepared: PreparedOperatorInput) -> str:
    """Hash deterministic local preparation facts for one immutable unit request."""
    payload = {
        "request_digest": prepared.digest,
        "unit": prepared.local.unit.model_dump(mode="json"),
        "image_digest": prepared.image.digest,
        "manifest_digest": prepared.image.manifest_digest,
        "receipt_inventory_digest": prepared.receipt.inventory_digest,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _set_deadline(conn: psycopg.Connection, valid_until: AwareDatetime) -> None:
    """Bound one caller-owned database transaction by the original dispatch deadline."""
    remaining_ms = int((valid_until - _now()).total_seconds() * 1000)
    if remaining_ms <= 0:
        raise ReleaseRejectedError("prepared dispatch deadline expired; no services were stopped")
    conn.execute("SELECT set_config('statement_timeout',%s,true)", (str(remaining_ms),))


def _run_prepared_participant(
    prepared: PreparedOperatorInput, projection: ObserverProjection
) -> int:
    """Bind and record one local unit's preflight; never acquire or reattach an operation.

    Any participant may call this with its validated immutable request and the
    pre-projected database credentials. It only accepts the coordinator's exact
    operation before the original deadline and refuses without stopping services.
    """
    from shared.prepared_rollout import (
        bind_prepared_participant,
        record_prepared_preflight,
        require_all_prepared_preflights,
        require_prepared_dispatch,
    )

    request = prepared.request
    with psycopg.connect(
        projection.db_url.get_secret_value(),
        autocommit=True,
        connect_timeout=_connect_timeout(request.valid_until),
    ) as conn:
        operation = None
        while _now() < request.valid_until:
            try:
                with conn.transaction():
                    _set_deadline(conn, request.valid_until)
                    operation = bind_prepared_participant(
                        conn,
                        request_id=request.request_id,
                        request_digest=prepared.digest,
                        valid_until=request.valid_until,
                        unit=prepared.local.unit,
                    )
            except ManagedWriterBarrierError:
                time.sleep(0.2)
                continue
            break
        if operation is None:
            sys.stderr.write("prepared dispatch deadline expired; no services were stopped\n")
            return 2
        with conn.transaction():
            _set_deadline(conn, request.valid_until)
            pending = require_prepared_dispatch(
                conn, operation, request.request_id, prepared.digest, prepared.local.unit
            )
        if pending.dispatch is None:
            raise ManagedWriterBarrierError("prepared dispatch disappeared")
        evidence_digest = _prepared_evidence_digest(prepared)
        previous = next(
            (item for item in pending.dispatch.preflights if item.unit == prepared.local.unit), None
        )
        if previous is None or previous.evidence_digest != evidence_digest:
            evidence = PreparedUnitPreflight(
                unit=prepared.local.unit,
                request_digest=prepared.digest,
                evidence_digest=evidence_digest,
                # Exact retries reproduce the same evidence; this is a bind-time
                # operation identity, not a renewed local observation timestamp.
                observed_at=operation.acquired_at,
            )
            with conn.transaction():
                _set_deadline(conn, request.valid_until)
                record_prepared_preflight(conn, operation, request.request_id, evidence)
        while _now() < request.valid_until:
            try:
                with conn.transaction():
                    _set_deadline(conn, request.valid_until)
                    require_all_prepared_preflights(
                        conn,
                        operation,
                        request.request_id,
                        prepared.digest,
                        prepared.local.unit,
                    )
            except ManagedWriterBarrierError:
                time.sleep(0.2)
                continue
            sys.stdout.write(
                f"prepared barrier complete for {prepared.local.unit.machine} "
                f"{prepared.local.unit.home}; operation {operation.holder}\n"
            )
            return 0
    sys.stderr.write("prepared dispatch deadline expired; no services were stopped\n")
    return 2


def _recover_or_refuse(
    conn: psycopg.Connection,
    prepared: PreparedOperatorInput,
    dispatch: PreparedDispatch,
    cause: ManagedWriterBarrierError,
    projection: ObserverProjection | None = None,
) -> int:
    """Recover a coordinator's exact abandoned operation only with fresh closure.

    The coordinator may call this after failed creation. It refuses a missing
    predecessor or a plan without its new collection; it never renews an old
    lease, and it delegates no service start beyond the participant preflight.
    """
    from shared.prepared_rollout import read_prepared_blockage, recover_prepared_operation

    with conn.transaction():
        _set_deadline(conn, prepared.request.valid_until)
        blockage = read_prepared_blockage(conn)
    if blockage.operation is None:
        sys.stderr.write(f"prepared update refused: {cause}\n")
        return 2
    if prepared.request.recovery_collection is None:
        sys.stderr.write(
            "prepared update refused: existing prepared operation "
            f"holder={blockage.operation.holder} acquired_at={blockage.operation.acquired_at} "
            f"target_sha={blockage.operation.target_sha} requires recovery with a NEW lawful "
            "operation (new plan with a new request_id), exact predecessor CAS, and a fresh "
            "all-unit writer closure\n"
        )
        return 2
    home = Path(prepared.local.unit.home)
    collection_path = Path(prepared.request.recovery_collection)
    collection = ManagedWriterCollection.model_validate_json(_private_plan(collection_path, home))
    with conn.transaction():
        _set_deadline(conn, prepared.request.valid_until)
        operation = recover_prepared_operation(
            conn,
            abandoned=blockage.operation,
            dispatch=dispatch,
            plan=prepared.request.normal,
            target_sha=prepared.request.target_sha,
            fresh_collection=collection,
        )
    sys.stdout.write(
        "prepared operation recovered: new operation "
        f"{operation.holder} replaced {blockage.operation.holder} via exact predecessor CAS\n"
    )
    if projection is None:
        projection = ObserverProjection.from_environment()
    rc = _run_prepared_participant(prepared, projection)
    if rc != 0:
        sys.stderr.write(
            "prepared dispatch barrier incomplete; operation remains pending until explicit "
            "recovery — no services were stopped\n"
        )
    return rc


def _run_prepared_coordinator(
    prepared: PreparedOperatorInput, projection: ObserverProjection
) -> int:
    """Create the sole prepared operation at the verified coordinator, then participate.

    Only the local unit named as coordinator may call this. Database checks
    independently enforce the unique gateway and roster; recovery is explicit,
    and no post-barrier cutover is authorized here.
    """
    from shared.prepared_rollout import create_prepared_operation

    request = prepared.request
    machine = regular_bytes(Path(prepared.local.unit.home) / "machine_name").decode().strip()
    holder = f"prepared:{machine}:pid{os.getpid()}"
    dispatch = PreparedDispatch(
        request_id=request.request_id,
        request_digest=prepared.digest,
        coordinator=request.coordinator,
        valid_until=request.valid_until,
    )
    with psycopg.connect(
        projection.db_url.get_secret_value(),
        autocommit=True,
        connect_timeout=_connect_timeout(request.valid_until),
    ) as conn:
        try:
            with conn.transaction():
                _set_deadline(conn, request.valid_until)
                operation = create_prepared_operation(
                    conn,
                    dispatch=dispatch,
                    plan=request.normal,
                    target_sha=request.target_sha,
                    holder=holder,
                )
        except ManagedWriterBarrierError as exc:
            return _recover_or_refuse(conn, prepared, dispatch, exc, projection)
    sys.stdout.write(
        "prepared operation created: "
        f"holder={operation.holder} target_sha={operation.target_sha} "
        f"expires_at={request.valid_until}\n"
    )
    rc = _run_prepared_participant(prepared, projection)
    if rc == 0:
        sys.stdout.write(
            "all-unit prepared barrier complete; maintenance phase owns the operation until "
            "finalization\n"
        )
    else:
        sys.stderr.write(
            "prepared dispatch barrier incomplete; operation remains pending until explicit "
            "recovery — no services were stopped\n"
        )
    return rc


def dispatch_prepared_update(prepared: PreparedOperatorInput) -> int:
    """Dispatch only the validated local coordinator or participant prepared leg.

    This reads the already-projected environment without Settings or a gateway
    fetch. It refuses missing projection variables and does not authorize any
    source cutover stage after the all-unit preflight barrier.
    """
    projection = ObserverProjection.from_environment()
    if prepared.local.unit == prepared.request.coordinator:
        return _run_prepared_coordinator(prepared, projection)
    return _run_prepared_participant(prepared, projection)


def run_prepared_update(args: argparse.Namespace) -> int:
    """Never silently reinterpret prepared flags as an ordinary source rollout."""
    if (
        not args.local
        or args.restart_only
        or args.force
        or args.dry_run
        or args.mode != "smooth"
        or args.origin is not None
        or args.rollout_log is not None
    ):
        sys.stderr.write(
            "prepared update requires --local alone; legacy rollout flags are refused\n"
        )
        return 2
    try:
        prepared = prepare_operator_input(Path(args.prepared))
        return dispatch_prepared_update(prepared)
    except (
        OSError,
        ValueError,
        ValidationError,
        ReleaseRejectedError,
        ManagedWriterBarrierError,
        psycopg.Error,
        KeyError,
    ) as exc:
        sys.stderr.write(f"prepared update refused: {exc}\n")
        return 2
