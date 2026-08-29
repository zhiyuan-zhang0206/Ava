"""Durable, crash-resumable state for explicit PITR activation."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from shared.private_storage import ensure_private_dir

ActivationPhase = Literal[
    "shadow",
    "snapshot_pending",
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

_SCHEMA_VERSION = 2
_PHASES = frozenset(
    {
        "shadow",
        "snapshot_pending",
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
    }
)
_EVIDENCE_PHASES = _PHASES - {"shadow", "snapshot_pending", "rolled_back"}


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
    pre_activation_credential_evidence: dict[str, str] | None = None
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
        raw_value: object = json.loads(payload)
        if not isinstance(raw_value, dict):
            raise TypeError("PITR activation record fields differ")
        raw = cast(dict[str, object], raw_value)
        if set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("PITR activation record fields differ")
        phase = raw["phase"]
        if not isinstance(phase, str) or phase not in _PHASES:
            raise ValueError("unknown PITR activation phase")

        def required_string(name: str) -> str:
            value = raw[name]
            if not isinstance(value, str):
                raise TypeError(f"PITR activation {name} must be a string")
            return value

        def optional_string(name: str) -> str | None:
            value = raw[name]
            if value is not None and not isinstance(value, str):
                raise ValueError(f"PITR activation {name} must be a string or null")
            return value

        def optional_string_map(name: str) -> dict[str, str] | None:
            value = raw[name]
            if value is None:
                return None
            if not isinstance(value, dict):
                raise TypeError(f"PITR activation {name} must be an object or null")
            items = cast(dict[object, object], value)
            if not all(
                isinstance(key, str) and isinstance(item, str) for key, item in items.items()
            ):
                raise ValueError(f"PITR activation {name} must contain string pairs")
            return cast(dict[str, str], value)

        schema_version = raw["schema_version"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise TypeError("PITR activation schema_version must be an integer")
        record = cls(
            schema_version=schema_version,
            operation_id=required_string("operation_id"),
            phase=cast(ActivationPhase, phase),
            started_at=required_string("started_at"),
            updated_at=required_string("updated_at"),
            origin=required_string("origin"),
            pre_activation_snapshot=optional_string("pre_activation_snapshot"),
            pre_activation_pg_settings=optional_string_map("pre_activation_pg_settings"),
            pre_activation_credential_evidence=optional_string_map(
                "pre_activation_credential_evidence"
            ),
            switched_wal=optional_string("switched_wal"),
            protected_manifest=optional_string("protected_manifest"),
            error=optional_string("error"),
        )
        if record.schema_version != _SCHEMA_VERSION:
            raise ValueError("unsupported PITR activation record schema")
        started = datetime.fromisoformat(record.started_at)
        updated = datetime.fromisoformat(record.updated_at)
        if started.tzinfo is None or updated.tzinfo is None:
            raise ValueError("PITR activation timestamps must carry timezone")
        if started.utcoffset() != UTC.utcoffset(started) or updated.utcoffset() != UTC.utcoffset(
            updated
        ):
            raise ValueError("PITR activation timestamps must be UTC")
        if updated < started:
            raise ValueError("PITR activation updated_at precedes started_at")
        if record.phase in _EVIDENCE_PHASES and (
            not record.pre_activation_snapshot
            or not record.pre_activation_pg_settings
            or not record.pre_activation_credential_evidence
        ):
            raise ValueError("PITR activation phase is missing logical recovery evidence")
        if record.phase == "snapshot_pending" and (
            not record.pre_activation_pg_settings or not record.pre_activation_credential_evidence
        ):
            raise ValueError("PITR activation snapshot phase is missing shadow evidence")
        if record.phase == "protected" and not record.protected_manifest:
            raise ValueError("protected PITR activation is missing its manifest")
        return record

    def advance(self, phase: ActivationPhase, **changes: object) -> ActivationRecord:
        raw = asdict(self)
        raw.update(changes)
        raw["phase"] = phase
        raw["updated_at"] = datetime.now(UTC).isoformat()
        return ActivationRecord.from_json(json.dumps(raw))


def activation_root(home: Path) -> Path:
    return home / "physical-backup" / "activation"


def record_path(home: Path) -> Path:
    return activation_root(home) / "operation.json"


def lock_path(home: Path) -> Path:
    return activation_root(home) / "operation.lock"


def load_record(home: Path) -> ActivationRecord | None:
    path = record_path(home)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("PITR activation record is not a regular file")
    return ActivationRecord.from_json(path.read_text())


def write_record(home: Path, record: ActivationRecord) -> None:
    ActivationRecord.from_json(json.dumps(asdict(record)))
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
