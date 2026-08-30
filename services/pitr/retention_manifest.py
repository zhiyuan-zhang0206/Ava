"""Canonical, local-only evidence for a PITR retention dry run."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from services.pitr.checksums import CRC32C, KNOWN_CHECKSUM_ALGOS

PLAN_SCHEMA_VERSION = 1


@dataclass(frozen=True, order=True)
class RetentionObject:
    object_name: str
    pin_token: str
    size: int
    archive_name: str | None
    kind: str
    checksum_algo: str
    checksum_value: str
    metadata: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not self.object_name or not self.pin_token or self.size <= 0 or not self.checksum_value:
            raise ValueError("retention object lacks an exact immutable identity")
        if self.kind not in {"base", "wal", "history"}:
            raise ValueError("retention object kind is unsupported")
        if self.checksum_algo not in KNOWN_CHECKSUM_ALGOS:
            raise ValueError("retention object checksum algorithm is unsupported")
        if tuple(sorted(self.metadata)) != self.metadata:
            raise ValueError("retention object metadata must be canonical")


@dataclass(frozen=True)
class RetentionDecision:
    object: RetentionObject
    reason: str


@dataclass(frozen=True)
class RetentionPlan:
    schema_version: int
    retained_chain_count: int
    evidence_sha256: str
    protected_chain_ids: tuple[str, ...]
    unprotected_chain_ids: tuple[str, ...]
    oldest_retained_chain_id: str | None
    ack_high_water: str | None
    blocked_reasons: tuple[str, ...]
    retained: tuple[RetentionDecision, ...]
    eligible: tuple[RetentionDecision, ...]
    retained_bytes: int
    eligible_bytes: int

    def __post_init__(self) -> None:
        if self.schema_version != PLAN_SCHEMA_VERSION or self.retained_chain_count < 2:
            raise ValueError("retention plan schema or chain count is invalid")
        if tuple(sorted(set(self.blocked_reasons))) != self.blocked_reasons:
            raise ValueError("retention plan blockers must be canonical")
        if self.blocked_reasons and self.eligible:
            raise ValueError("a blocked retention plan cannot contain eligible objects")
        if self.retained_bytes != sum(item.object.size for item in self.retained):
            raise ValueError("retained byte total differs from its decisions")
        if self.eligible_bytes != sum(item.object.size for item in self.eligible):
            raise ValueError("eligible byte total differs from its decisions")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    @classmethod
    def from_json(cls, value: str) -> RetentionPlan:
        raw: dict[str, Any] = json.loads(value)
        if set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("retention plan fields do not match schema")
        raw["protected_chain_ids"] = tuple(raw["protected_chain_ids"])
        raw["unprotected_chain_ids"] = tuple(raw["unprotected_chain_ids"])
        raw["blocked_reasons"] = tuple(raw["blocked_reasons"])
        raw["retained"] = tuple(_decision(item) for item in raw["retained"])
        raw["eligible"] = tuple(_decision(item) for item in raw["eligible"])
        return cls(**raw)


def _decision(raw: dict[str, Any]) -> RetentionDecision:
    if set(raw) != {"object", "reason"}:
        raise ValueError("retention decision fields do not match schema")
    raw_object = dict(raw["object"])
    # Legacy normalization: dry-run plans written before the store
    # abstraction carry ``generation`` + ``crc32c`` (the GCS vocabulary).
    if "pin_token" not in raw_object:
        legacy_generation = raw_object.pop("generation", None)
        if legacy_generation is None:
            raise ValueError("retention object lacks a pin token")
        raw_object["pin_token"] = str(legacy_generation)
    else:
        raw_object.pop("generation", None)
    if "checksum_algo" not in raw_object:
        raw_object["checksum_algo"] = CRC32C
    if "checksum_value" not in raw_object:
        legacy_crc32c = raw_object.pop("crc32c", None)
        if legacy_crc32c is None:
            raise ValueError("retention object lacks a checksum")
        raw_object["checksum_value"] = legacy_crc32c
    else:
        raw_object.pop("crc32c", None)
    raw_object["metadata"] = tuple(tuple(item) for item in raw_object["metadata"])
    return RetentionDecision(RetentionObject(**raw_object), str(raw["reason"]))
