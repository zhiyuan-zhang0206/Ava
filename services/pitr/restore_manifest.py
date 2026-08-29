"""Generation-pinned restore plan and protected-proof manifest."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from services.pitr.base_manifest import CandidateManifest, WalRange, _lsn
from services.pitr.uploader import AckManifest

PROTECTED_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RestoreObject:
    archive_name: str
    object_name: str
    generation: int
    size: int
    crc32c: str
    metadata: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.generation <= 0 or self.size <= 0 or not self.crc32c:
            raise ValueError("restore object lacks an immutable generation identity")
        if tuple(sorted(self.metadata)) != self.metadata:
            raise ValueError("restore object metadata must be canonical")


@dataclass(frozen=True)
class RestoreProof:
    run_id: str
    started_at: str
    completed_at: str
    target_lsn: str
    achieved_lsn: str
    live_postgres_pid: int
    live_probe_sha256: str
    candidate_verify_evidence_sha256: str
    replay_seconds: float
    smoke_seconds: float
    restored_verify_seconds: float
    downloaded_bytes: int
    restored_fingerprint_sha256: str

    def __post_init__(self) -> None:
        try:
            started = datetime.fromisoformat(self.started_at)
            completed = datetime.fromisoformat(self.completed_at)
        except ValueError as exc:
            raise ValueError("restore proof timestamps must be ISO 8601") from exc
        if started.tzinfo is None or completed.tzinfo is None:
            raise ValueError("restore proof timestamps must be timezone-aware")
        if completed < started:
            raise ValueError("restore proof completion precedes its start")


@dataclass(frozen=True)
class ProtectedManifest:
    schema_version: int
    protected: bool
    chain_id: str
    candidate_sha256: str
    candidate: CandidateManifest
    base: RestoreObject
    wal: tuple[RestoreObject, ...]
    target_lsn: str
    wal_segment_size: int
    proof: RestoreProof

    def __post_init__(self) -> None:
        if self.schema_version != PROTECTED_SCHEMA_VERSION or not self.protected:
            raise ValueError("protected manifest must use the supported protected schema")
        if self.candidate.protected or self.chain_id != self.candidate.chain_id:
            raise ValueError("protected proof must reference an immutable candidate")
        if self.candidate_sha256 != candidate_sha256(self.candidate):
            raise ValueError("protected candidate digest differs from the candidate")
        if self.base.object_name != self.candidate.base_object.object_name:
            raise ValueError("protected base differs from the candidate")
        if self.base.generation != self.candidate.base_object.generation:
            raise ValueError("protected base generation differs from the candidate")
        if _lsn(self.target_lsn) < _lsn(self.candidate.end_lsn):
            raise ValueError("restore proof did not reach the candidate target")
        if self.proof.target_lsn != self.target_lsn:
            raise ValueError("restore proof target differs from the protected manifest")
        if _lsn(self.proof.achieved_lsn) < _lsn(self.target_lsn):
            raise ValueError("restore proof did not achieve its target LSN")
        if self.wal_segment_size != self.candidate.wal_segment_size:
            raise ValueError("protected WAL segment size differs from the candidate")
        expected = required_archive_names(self.candidate.wal_ranges, self.wal_segment_size)
        if tuple(item.archive_name for item in self.wal) != expected:
            raise ValueError("protected WAL objects are not the exact required sequence")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, value: str) -> ProtectedManifest:
        raw: dict[str, Any] = json.loads(value)
        if set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("protected manifest fields do not match schema")
        raw["candidate"] = CandidateManifest.from_json(json.dumps(raw["candidate"]))
        raw["base"] = _restore_object(raw["base"])
        raw["wal"] = tuple(_restore_object(item) for item in raw["wal"])
        raw["proof"] = RestoreProof(**raw["proof"])
        return cls(**raw)


def _restore_object(raw: dict[str, Any]) -> RestoreObject:
    if set(raw) != set(RestoreObject.__dataclass_fields__):
        raise ValueError("restore object fields do not match schema")
    raw["metadata"] = tuple(tuple(str(value) for value in item) for item in raw["metadata"])
    return RestoreObject(**raw)


def candidate_sha256(candidate: CandidateManifest) -> str:
    return hashlib.sha256(candidate.to_json().encode()).hexdigest()


def required_archive_names(ranges: tuple[WalRange, ...], segment_size: int) -> tuple[str, ...]:
    if segment_size <= 0 or segment_size & (segment_size - 1):
        raise ValueError("WAL segment size must be a positive power of two")
    segments_per_log = 0x100000000 // segment_size
    names: list[str] = []
    seen_timelines: set[int] = set()
    for item in ranges:
        if item.timeline <= 0:
            raise ValueError("WAL timeline must be positive")
        if item.timeline > 1 and item.timeline not in seen_timelines:
            names.append(f"{item.timeline:08X}.history")
        seen_timelines.add(item.timeline)
        start = _lsn(item.start_lsn) // segment_size
        end_value = _lsn(item.end_lsn)
        if end_value <= _lsn(item.start_lsn):
            raise ValueError("WAL range must advance")
        end = (end_value - 1) // segment_size
        for segment in range(start, end + 1):
            log = segment // segments_per_log
            offset = segment % segments_per_log
            names.append(f"{item.timeline:08X}{log:08X}{offset:08X}")
    if len(names) != len(set(names)):
        raise ValueError("WAL ranges map to duplicate archive names")
    return tuple(names)


def wal_objects_from_acks(
    *, ack_dir: Path, archive_names: tuple[str, ...]
) -> tuple[RestoreObject, ...]:
    objects: list[RestoreObject] = []
    for archive_name in archive_names:
        path = ack_dir / f"{archive_name}.ack.json"
        try:
            raw = json.loads(path.read_text())
            ack = AckManifest(**raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"required archive lacks a valid ACK: {archive_name}") from exc
        if ack.archive_name != archive_name or ack.generation <= 0:
            raise ValueError(f"required archive ACK identity differs: {archive_name}")
        metadata = {
            "ava-archive-name": ack.archive_name,
            "ava-source-sha256": ack.source_sha256,
            "ava-source-size": str(ack.source_size),
            "ava-ciphertext-crc32c": ack.ciphertext_crc32c,
            "ava-encryption-format": ack.encryption_format,
            "ava-key-id": ack.key_id,
        }
        objects.append(
            RestoreObject(
                archive_name,
                ack.object_name,
                ack.generation,
                ack.ciphertext_size,
                ack.ciphertext_crc32c,
                tuple(sorted(metadata.items())),
            )
        )
    return tuple(objects)
