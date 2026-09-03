"""Explicit operator admission for a retained image, before settings or source.

Image and expected inventory are not a bootable rollback or all-writer closure.
This entry deliberately refuses an unimplemented first-cutover contract before
creating an updater session, taking a deployment lease, or stopping a service.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, ValidationError, model_validator

from shared.managed_writer_barrier import Digest, EvidenceModel
from shared.managed_writer_publication import NormalStartPlan, PublishedUnit
from shared.runtime_interpreter import WHEEL_RUNTIME, runtime_venv
from shared.runtime_publication_input import PreparationReceipt
from shared.runtime_release import (
    ReleaseRejectedError,
    VerifiedRelease,
    file_sha256,
    verify_release,
)
from shared.verified_file import regular_bytes


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
        # Do not convert the existing inventory's unknown closure into permission.
        # First normal-source quiesce/LKG and all-unit coordination remain real
        # missing producers, not optional flags an operator may assert in JSON.
        sys.stderr.write(
            "first source cutover is not implemented: requires fresh-operation native "
            "handoff, bootable normal LKG, full unit/job closure and checked recovery; "
            f"verified preparation {prepared.digest} did not stop or change any service\n"
        )
        return 2
    except (OSError, ValueError, ValidationError, ReleaseRejectedError) as exc:
        sys.stderr.write(f"prepared update refused: {exc}\n")
        return 2
