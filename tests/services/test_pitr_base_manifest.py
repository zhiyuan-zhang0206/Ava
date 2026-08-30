from __future__ import annotations

import json

import pytest

from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange


def _manifest() -> CandidateManifest:
    return CandidateManifest(
        schema_version=1,
        chain_id="20260829T000000Z",
        protected=False,
        postgres_major=17,
        database_name="ava",
        system_identifier="1",
        wal_segment_size=16 * 1024 * 1024,
        timeline=1,
        start_lsn="0/1000000",
        end_lsn="0/3000000",
        wal_ranges=(
            WalRange(1, "0/1000000", "0/2000000"),
            WalRange(2, "0/2000000", "0/3000000"),
        ),
        base_object=BaseObject("base", "1", 10, "crc32c", "crc", "sha", 5, "key", "AVAPITRB1"),
        native_manifest_sha256="manifest",
        native_manifest_member_path="backup_manifest",
        native_manifest_container_object_name="base",
        native_manifest_container_pin_token="1",  # noqa: S106 — test fixture
        migration_set_sha256="migrations",
    )


def test_candidate_is_strict_and_never_protected() -> None:
    manifest = _manifest()
    raw = json.loads(manifest.to_json())
    raw["unknown"] = True
    with pytest.raises(ValueError, match="fields"):
        CandidateManifest.from_json(json.dumps(raw))
    raw.pop("unknown")
    raw["protected"] = True
    with pytest.raises(ValueError, match="cannot publish protected"):
        CandidateManifest.from_json(json.dumps(raw))


def test_candidate_rejects_wal_gap() -> None:
    raw = json.loads(_manifest().to_json())
    raw["wal_ranges"][1]["start_lsn"] = "0/2100000"
    with pytest.raises(ValueError, match="gap"):
        CandidateManifest.from_json(json.dumps(raw))
