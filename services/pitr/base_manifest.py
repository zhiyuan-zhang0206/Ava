"""Strict, unprotected manifest for one physical base-backup candidate."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from services.pitr.checksums import CRC32C
from services.pitr.object_store import RemoteObjectAck

SCHEMA_VERSION = 1


def base_object_from_legacy(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a pre-abstraction BaseObject shape in place.

    Manifests written before the store abstraction carry ``generation``
    and ``ciphertext_crc32c`` (the GCS vocabulary); the same renames that
    produced the new fields must not make pre-existing on-disk manifests
    unreadable. The legacy keys map one-to-one onto ``pin_token`` /
    ``ciphertext_checksum_*`` without ambiguity.
    """
    normalized = dict(raw)
    if "pin_token" not in normalized:
        legacy_generation = normalized.pop("generation", None)
        if legacy_generation is None:
            raise TypeError("base object lacks a pin token")
        normalized["pin_token"] = str(legacy_generation)
    else:
        normalized.pop("generation", None)
    if "ciphertext_checksum_algo" not in normalized:
        normalized["ciphertext_checksum_algo"] = CRC32C
    if "ciphertext_checksum_value" not in normalized:
        legacy_crc32c = normalized.pop("ciphertext_crc32c", None)
        if legacy_crc32c is None:
            raise TypeError("base object lacks a ciphertext checksum")
        normalized["ciphertext_checksum_value"] = legacy_crc32c
        normalized["ciphertext_crc32c"] = legacy_crc32c
    return normalized


@dataclass(frozen=True)
class WalRange:
    timeline: int
    start_lsn: str
    end_lsn: str


@dataclass(frozen=True)
class BaseObject:
    object_name: str
    pin_token: str
    ciphertext_size: int
    ciphertext_crc32c: str
    ciphertext_checksum_algo: str
    ciphertext_checksum_value: str
    source_sha256: str
    source_size: int
    key_id: str
    encryption_format: str


@dataclass(frozen=True)
class CandidateManifest:
    schema_version: int
    chain_id: str
    protected: bool
    postgres_major: int
    database_name: str
    system_identifier: str
    wal_segment_size: int
    timeline: int
    start_lsn: str
    end_lsn: str
    wal_ranges: tuple[WalRange, ...]
    base_object: BaseObject
    native_manifest_sha256: str
    native_manifest_member_path: str
    native_manifest_container_object_name: str
    native_manifest_container_pin_token: str
    migration_set_sha256: str

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported base candidate manifest version")
        if self.protected:
            raise ValueError("base candidate foundation cannot publish protected manifests")
        if self.wal_segment_size <= 0 or self.wal_segment_size & (self.wal_segment_size - 1):
            raise ValueError("base candidate WAL segment size must be a positive power of two")
        if not self.wal_ranges:
            raise ValueError("base candidate has no required WAL range")
        if self.wal_ranges[0].start_lsn != self.start_lsn:
            raise ValueError("candidate start LSN differs from its WAL ranges")
        if self.wal_ranges[-1].end_lsn != self.end_lsn:
            raise ValueError("candidate end LSN differs from its WAL ranges")
        for previous, current in zip(self.wal_ranges, self.wal_ranges[1:], strict=False):
            if _lsn(previous.end_lsn) != _lsn(current.start_lsn):
                raise ValueError("candidate WAL ranges contain a gap")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> CandidateManifest:
        raw: dict[str, Any] = dict(json.loads(value))
        # Legacy normalization (pre-store-abstraction shape): manifests
        # already on disk must stay readable through the field renames —
        # a live activation candidate predates them (QA #1131 block 1).
        if "native_manifest_container_pin_token" not in raw:
            legacy = raw.pop("native_manifest_container_generation", None)
            if legacy is None:
                raise ValueError("base candidate manifest lacks a container pin token")
            raw["native_manifest_container_pin_token"] = str(legacy)
        else:
            raw.pop("native_manifest_container_generation", None)
        if isinstance(raw.get("base_object"), dict):
            raw["base_object"] = base_object_from_legacy(raw["base_object"])
        expected = set(cls.__dataclass_fields__)
        if set(raw) != expected:
            raise ValueError("base candidate manifest fields do not match schema")
        raw["wal_ranges"] = tuple(WalRange(**item) for item in raw["wal_ranges"])
        raw["base_object"] = BaseObject(**raw["base_object"])
        return cls(**raw)


def base_object_from_ack(
    ack: RemoteObjectAck,
    *,
    ciphertext_crc32c: str,
    source_sha256: str,
    source_size: int,
    key_id: str,
    encryption_format: str,
) -> BaseObject:
    return BaseObject(
        object_name=ack.object_name,
        pin_token=ack.pin_token,
        ciphertext_size=ack.size,
        ciphertext_crc32c=ciphertext_crc32c,
        ciphertext_checksum_algo=ack.checksum.algo,
        ciphertext_checksum_value=ack.checksum.value,
        source_sha256=source_sha256,
        source_size=source_size,
        key_id=key_id,
        encryption_format=encryption_format,
    )


def _lsn(value: str) -> int:
    try:
        high, low = value.split("/", 1)
        return (int(high, 16) << 32) | int(low, 16)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid PostgreSQL LSN: {value}") from exc


def parse_native_manifest(path: Path) -> tuple[str, str, tuple[WalRange, ...]]:
    """Return system id and exact LSN coverage from PostgreSQL's manifest."""

    raw = json.loads(path.read_text())
    allowed = {
        "PostgreSQL-Backup-Manifest-Version",
        "System-Identifier",
        "Files",
        "WAL-Ranges",
        "Manifest-Checksum",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown PostgreSQL backup manifest fields: {sorted(unknown)}")
    ranges = tuple(
        WalRange(
            timeline=int(item["Timeline"]),
            start_lsn=str(item["Start-LSN"]),
            end_lsn=str(item["End-LSN"]),
        )
        for item in raw["WAL-Ranges"]
    )
    if not ranges:
        raise ValueError("PostgreSQL backup manifest has no WAL ranges")
    return str(raw["System-Identifier"]), ranges[0].start_lsn, ranges
