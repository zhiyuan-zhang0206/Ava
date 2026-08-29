"""Durable, crash-resumable state for explicit PITR activation."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from shared.private_storage import ensure_private_dir

ActivationPhase = Literal[
    "shadow",
    "snapshot_verified",
    "wal_config_pending",
    "wal_restart_pending",
    "wal_ack_pending",
    "wal_remote_verified",
    "base_pending",
    "restore_pending",
    "protected",
    "rollback_pending",
    "rolled_back",
]

_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ActivationRecord:
    schema_version: int
    operation_id: str
    phase: ActivationPhase
    started_at: str
    updated_at: str
    origin: str
    pre_activation_snapshot: str | None = None
    pre_activation_pg_settings: dict[str, str] | None = None
    switched_wal: str | None = None
    protected_manifest: str | None = None
    error: str | None = None

    @classmethod
    def start(cls, *, operation_id: str, origin: str) -> ActivationRecord:
        now = datetime.now(UTC).isoformat()
        return cls(
            schema_version=_SCHEMA_VERSION,
            operation_id=operation_id,
            phase="shadow",
            started_at=now,
            updated_at=now,
            origin=origin,
        )

    @classmethod
    def from_json(cls, payload: str) -> ActivationRecord:
        raw = json.loads(payload)
        if not isinstance(raw, dict) or set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("PITR activation record fields differ")
        record = cls(**raw)
        if record.schema_version != _SCHEMA_VERSION:
            raise ValueError("unsupported PITR activation record schema")
        datetime.fromisoformat(record.started_at)
        datetime.fromisoformat(record.updated_at)
        return record

    def advance(self, phase: ActivationPhase, **changes: object) -> ActivationRecord:
        raw = asdict(self)
        raw.update(changes)
        raw["phase"] = phase
        raw["updated_at"] = datetime.now(UTC).isoformat()
        return ActivationRecord(**raw)


def activation_root(home: Path) -> Path:
    return home / "physical-backup" / "activation"


def record_path(home: Path) -> Path:
    return activation_root(home) / "operation.json"


def load_record(home: Path) -> ActivationRecord | None:
    path = record_path(home)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("PITR activation record is not a regular file")
    return ActivationRecord.from_json(path.read_text())


def write_record(home: Path, record: ActivationRecord) -> None:
    directory = ensure_private_dir(activation_root(home))
    path = record_path(home)
    fd, raw = tempfile.mkstemp(prefix=".operation-", suffix=".partial", dir=directory)
    partial = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(asdict(record), output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        partial.chmod(0o600)
        partial.replace(path)
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        partial.unlink(missing_ok=True)
