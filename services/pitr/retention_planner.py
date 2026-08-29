"""Read local immutable evidence and publish a durable dry-run retention plan."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from services.pitr.base_manifest import CandidateManifest
from services.pitr.restore_manifest import ProtectedManifest
from services.pitr.retention_inventory import RetentionInventoryReader
from services.pitr.retention_manifest import RetentionObject, RetentionPlan
from services.pitr.retention_policy import RetentionEvidence, plan_retention
from services.pitr.uploader import AckManifest


@dataclass(frozen=True)
class DryRunResult:
    path: Path
    digest: str
    blocked: bool
    retained_objects: int
    eligible_objects: int
    retained_bytes: int
    eligible_bytes: int


def build_local_evidence(
    root: Path, *, inventory_reader: RetentionInventoryReader | None = None
) -> RetentionEvidence:
    """Take a content-addressed local evidence snapshot without changing source state."""

    before = _evidence_fingerprint(root)
    malformed: list[str] = []
    candidates: list[CandidateManifest] = []
    protected: list[ProtectedManifest] = []
    inventory: list[RetentionObject] = []
    candidate_dir = root / "base-manifests"
    protected_dir = root / "protected-manifests"
    ack_dir = root / "ack"
    for path in _strict_files(candidate_dir, "*.candidate.json", malformed):
        try:
            candidate = CandidateManifest.from_json(path.read_text())
            candidates.append(candidate)
            base = candidate.base_object
            inventory.append(
                RetentionObject(
                    base.object_name,
                    base.generation,
                    base.ciphertext_size,
                    None,
                    "base",
                )
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            malformed.append(str(path))
    for path in _strict_files(protected_dir, "*.json", malformed):
        try:
            protected.append(ProtectedManifest.from_json(path.read_text()))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            malformed.append(str(path))
    for path in _strict_files(ack_dir, "*.ack.json", malformed):
        try:
            ack = AckManifest(**json.loads(path.read_text()))
            kind = "history" if ack.archive_name.endswith(".history") else "wal"
            inventory.append(
                RetentionObject(
                    ack.object_name,
                    ack.generation,
                    ack.ciphertext_size,
                    ack.archive_name,
                    kind,
                )
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            malformed.append(str(path))
    remote_before = inventory_reader.snapshot() if inventory_reader is not None else None
    remote_after = inventory_reader.snapshot() if inventory_reader is not None else None
    if remote_before is not None:
        inventory = list(remote_before.objects)
        malformed.extend(remote_before.unknown_names)
        if remote_after != remote_before:
            malformed.append("remote inventory changed during snapshot")
    after = _evidence_fingerprint(root)
    return RetentionEvidence(
        tuple(candidates),
        tuple(protected),
        tuple(inventory),
        tuple(sorted(set(malformed))),
        before,
        after,
    )


def write_dry_run_plan(
    root: Path,
    *,
    retain_chains: int = 2,
    inventory_reader: RetentionInventoryReader | None = None,
) -> DryRunResult:
    plan = plan_retention(
        build_local_evidence(root, inventory_reader=inventory_reader),
        retain_chains=retain_chains,
    )
    destination = root / "retention-plans" / "latest.dry-run.json"
    _atomic_bytes(destination, plan.to_json().encode())
    return DryRunResult(
        destination,
        plan.digest(),
        bool(plan.blocked_reasons),
        len(plan.retained),
        len(plan.eligible),
        plan.retained_bytes,
        plan.eligible_bytes,
    )


def inspect_dry_run_plan(root: Path) -> RetentionPlan:
    return RetentionPlan.from_json((root / "retention-plans" / "latest.dry-run.json").read_text())


def _strict_files(root: Path, pattern: str, malformed: list[str]) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    expected = set(root.glob(pattern))
    entries = set(root.iterdir())
    for entry in entries - expected:
        malformed.append(str(entry))
    for entry in expected:
        if entry.is_symlink() or not entry.is_file():
            malformed.append(str(entry))
    return tuple(sorted(entry for entry in expected if entry.is_file() and not entry.is_symlink()))


def _evidence_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for directory in ("base-manifests", "protected-manifests", "ack"):
        path = root / directory
        if not path.exists():
            continue
        for entry in sorted(path.iterdir()):
            digest.update(directory.encode())
            digest.update(entry.name.encode())
            info = entry.lstat()
            digest.update(f"{info.st_mode}:{info.st_size}:{info.st_mtime_ns}".encode())
            if entry.is_file() and not entry.is_symlink():
                digest.update(hashlib.sha256(entry.read_bytes()).digest())
    return digest.hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        staged.replace(path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staged.unlink(missing_ok=True)
