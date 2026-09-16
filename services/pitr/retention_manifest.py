"""Canonical evidence and decisions for a PITR retention dry run.

The vocabulary spans both deletion surfaces: the PITR prefix (base / WAL /
history archives) and the flat logical dump namespace (``kind="logical"``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from services.pitr.checksums import CRC32C, KNOWN_CHECKSUM_ALGOS

PLAN_SCHEMA_VERSION = 1

SIDECAR_SUFFIX = ".ack.json"
"""The ACK sidecar suffix OSS/Baidu publish beside a host object."""


@dataclass(frozen=True, order=True)
class RetentionSidecar:
    """One sidecar object with its own immutable identity.

    The deletion unit is the (host object, sidecar) pair: once the host's
    identity-bound delete has been observed, the sidecar is deleted through
    the same protocol with this recorded identity.
    """

    object_name: str
    pin_token: str
    size: int

    def __post_init__(self) -> None:
        if not self.object_name.endswith(SIDECAR_SUFFIX) or not self.pin_token or self.size <= 0:
            raise ValueError("retention sidecar lacks an exact immutable identity")

    def host_name(self) -> str:
        return self.object_name[: -len(SIDECAR_SUFFIX)]


@dataclass(frozen=True, order=True)
class SidecarPair:
    """An inventory observation: a live host object and its bound sidecar.

    ``host_pin_token`` is the host identity the sidecar content binds to;
    the policy attaches the sidecar to the host's decision only when it
    still equals the decision object's live pin token.
    """

    host_pin_token: str
    sidecar: RetentionSidecar


@dataclass(frozen=True, order=True)
class OrphanSidecar:
    """A sidecar whose host is gone, with the host reconstructed from the
    sidecar content so the normal eligibility predicates still apply."""

    host: RetentionObject
    sidecar: RetentionSidecar

    def __post_init__(self) -> None:
        if self.sidecar.host_name() != self.host.object_name:
            raise ValueError("orphan sidecar names a different host object")


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
        if self.kind not in {"base", "wal", "history", "logical"}:
            raise ValueError("retention object kind is unsupported")
        if self.checksum_algo not in KNOWN_CHECKSUM_ALGOS:
            raise ValueError("retention object checksum algorithm is unsupported")
        if tuple(sorted(self.metadata)) != self.metadata:
            raise ValueError("retention object metadata must be canonical")


@dataclass(frozen=True)
class RetentionDecision:
    object: RetentionObject
    reason: str
    sidecar: RetentionSidecar | None = None

    def __post_init__(self) -> None:
        if self.sidecar is not None and self.sidecar.host_name() != self.object.object_name:
            raise ValueError("retention decision sidecar names a different host object")


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
    orphan_sidecars: tuple[RetentionSidecar, ...] = ()
    weak_evidence: tuple[str, ...] = ()
    """Object names whose decision rests on weak evidence (strict naming and
    a live stat only, no verified sidecar binding) -- the honest annotation
    the logical-namespace design requires. Both retained and eligible
    decisions are listed; the names are canonical and unique."""

    def __post_init__(self) -> None:
        if self.schema_version != PLAN_SCHEMA_VERSION or self.retained_chain_count < 2:
            raise ValueError("retention plan schema or chain count is invalid")
        if tuple(sorted(set(self.blocked_reasons))) != self.blocked_reasons:
            raise ValueError("retention plan blockers must be canonical")
        if self.blocked_reasons and self.eligible:
            raise ValueError("a blocked retention plan cannot contain eligible objects")
        if self.blocked_reasons and self.orphan_sidecars:
            raise ValueError("a blocked retention plan cannot contain orphan sidecars")
        if self.retained_bytes != sum(item.object.size for item in self.retained):
            raise ValueError("retained byte total differs from its decisions")
        if self.eligible_bytes != sum(item.object.size for item in self.eligible):
            raise ValueError("eligible byte total differs from its decisions")
        if tuple(sorted(set(self.orphan_sidecars))) != self.orphan_sidecars:
            raise ValueError("orphan sidecars must be canonical and unique")
        if tuple(sorted(set(self.weak_evidence))) != self.weak_evidence:
            raise ValueError("weak evidence names must be canonical and unique")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    @classmethod
    def from_json(cls, value: str) -> RetentionPlan:
        raw: dict[str, Any] = json.loads(value)
        # Legacy normalization: dry-run plans written before the sidecar
        # rules carry no ``orphan_sidecars`` field (and decisions carry no
        # ``sidecar`` field); plans written before the logical namespace
        # carry no ``weak_evidence`` field.
        raw.setdefault("orphan_sidecars", [])
        raw.setdefault("weak_evidence", [])
        if set(raw) != set(cls.__dataclass_fields__):
            raise ValueError("retention plan fields do not match schema")
        raw["protected_chain_ids"] = tuple(raw["protected_chain_ids"])
        raw["unprotected_chain_ids"] = tuple(raw["unprotected_chain_ids"])
        raw["blocked_reasons"] = tuple(raw["blocked_reasons"])
        raw["retained"] = tuple(_decision(item) for item in raw["retained"])
        raw["eligible"] = tuple(_decision(item) for item in raw["eligible"])
        raw["orphan_sidecars"] = tuple(RetentionSidecar(**item) for item in raw["orphan_sidecars"])
        raw["weak_evidence"] = tuple(str(item) for item in raw["weak_evidence"])
        return cls(**raw)


def _decision(raw: dict[str, Any]) -> RetentionDecision:
    if not set(raw) <= {"object", "reason", "sidecar"} or not {"object", "reason"} <= set(raw):
        raise ValueError("retention decision fields do not match schema")
    raw_sidecar = raw.get("sidecar")
    sidecar = None if raw_sidecar is None else RetentionSidecar(**dict(raw_sidecar))
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
    return RetentionDecision(RetentionObject(**raw_object), str(raw["reason"]), sidecar)
