from __future__ import annotations

import json

import pytest

from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
from services.pitr.restore_manifest import (
    ProtectedManifest,
    RestoreObject,
    RestoreProof,
    required_archive_names,
)


def _candidate() -> CandidateManifest:
    return CandidateManifest(
        schema_version=1,
        chain_id="20260829T000000Z",
        protected=False,
        postgres_major=17,
        system_identifier="42",
        wal_segment_size=16 * 1024 * 1024,
        timeline=1,
        start_lsn="0/1000000",
        end_lsn="0/3000000",
        wal_ranges=(
            WalRange(1, "0/1000000", "0/2000000"),
            WalRange(2, "0/2000000", "0/3000000"),
        ),
        base_object=BaseObject("base", 7, 100, "crc", "sha", 80, "key", "AVAPITRB1"),
        native_manifest_sha256="native",
        native_manifest_member_path="backup_manifest",
        native_manifest_container_object_name="base",
        native_manifest_container_generation=7,
        migration_set_sha256="migrations",
    )


def _object(name: str, generation: int = 1) -> RestoreObject:
    return RestoreObject(name, f"wal/{name}", generation, 10, "crc", (("key", "value"),))


def test_archive_names_use_numeric_lsn_and_end_is_exclusive() -> None:
    assert required_archive_names(_candidate().wal_ranges, 16 * 1024 * 1024) == (
        "000000010000000000000001",
        "00000002.history",
        "000000020000000000000002",
    )


def test_protected_manifest_is_strict_and_requires_exact_wal_sequence() -> None:
    candidate = _candidate()
    names = required_archive_names(candidate.wal_ranges, candidate.wal_segment_size)
    proof = RestoreProof(
        "run",
        "start",
        "end",
        candidate.end_lsn,
        candidate.end_lsn,
        1,
        "live",
        1,
        2,
        3,
        4,
        100,
        "restored",
    )
    manifest = ProtectedManifest(
        1,
        True,
        candidate.chain_id,
        "candidate",
        candidate,
        RestoreObject("base", "base", 7, 100, "crc", (("key", "value"),)),
        tuple(_object(name) for name in names),
        candidate.end_lsn,
        candidate.wal_segment_size,
        proof,
    )
    raw = json.loads(manifest.to_json())
    raw["unknown"] = True
    with pytest.raises(ValueError, match="fields"):
        ProtectedManifest.from_json(json.dumps(raw))
    raw.pop("unknown")
    raw["wal"].pop()
    with pytest.raises(ValueError, match="exact required sequence"):
        ProtectedManifest.from_json(json.dumps(raw))


def test_archive_names_reject_gap_like_duplicate_segment_projection() -> None:
    ranges = (
        WalRange(1, "0/1000000", "0/1800000"),
        WalRange(1, "0/1800000", "0/2000000"),
    )
    with pytest.raises(ValueError, match="duplicate"):
        required_archive_names(ranges, 16 * 1024 * 1024)
