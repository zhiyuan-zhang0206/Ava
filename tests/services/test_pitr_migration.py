"""Record-rewrite tests for scripts/pitr_migrate_gcs_to_baidu.py.

The GCS -> Baidu migration rewrites local identity records field-level:
ACKs, candidate manifests, and protected manifests must swap their GCS
vocabulary (generation + crc32c) for Baidu pins (fs_id:md5) while keeping
every other byte of the original serialization — these tests pin both
shapes and the fail-closed path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

_MOD_PATH = Path(__file__).resolve().parents[2] / "scripts" / "pitr_migrate_gcs_to_baidu.py"
_MOD_NAME = "pitr_migrate_under_test"
_spec = importlib.util.spec_from_file_location(_MOD_NAME, _MOD_PATH)
assert _spec and _spec.loader
migrate = importlib.util.module_from_spec(_spec)
sys.modules[_MOD_NAME] = migrate
_spec.loader.exec_module(migrate)


def _mapping() -> dict[str, dict[str, str]]:
    return {
        "ava-pitr/base/20260830T043835Z/ab/base.tar.zst.enc": {
            "object_name": "ava-pitr/base/20260830T043835Z/ab/base.tar.zst.enc",
            "gcs_generation": "1788085003231815",
            "gcs_crc32c": "viqqbw==",
            "baidu_pin": "100:basemd5",
            "size": "6319665156",
            "md5": "basemd5",
        },
        "ava-pitr/wal/00000001/000000010000000000000001.enc": {
            "object_name": "ava-pitr/wal/00000001/000000010000000000000001.enc",
            "gcs_generation": "999",
            "gcs_crc32c": "walcrc",
            "baidu_pin": "200:wal-md5",
            "size": "16777216",
            "md5": "wal-md5",
        },
    }


# ── ACK rewrite ──


def test_rewrite_ack_legacy_shape_keeps_the_plan_digest() -> None:
    raw: dict[str, Any] = {
        "archive_name": "000000010000000000000001",
        "source_sha256": "a" * 64,
        "source_size": 16,
        "object_name": "ava-pitr/wal/00000001/000000010000000000000001.enc",
        "generation": 999,
        "ciphertext_size": 100,
        "ciphertext_crc32c": "walcrc",
        "encryption_format": "AVAPITR1",
        "key_id": "v1",
        "acknowledged_at": "2026-08-30T10:00:00+00:00",
    }
    migrate._rewrite_ack(raw, _mapping())
    assert raw["pin_token"] == "200:wal-md5"  # noqa: S105 — fixture identity
    assert raw["ciphertext_checksum_algo"] == "md5"
    assert raw["ciphertext_checksum_value"] == "wal-md5"
    assert "generation" not in raw
    # the local plan digest is backend-independent and stays untouched
    assert raw["ciphertext_crc32c"] == "walcrc"
    assert raw["archive_name"] == "000000010000000000000001"


def test_rewrite_ack_fresh_shape_swaps_identity() -> None:
    raw: dict[str, Any] = {
        "archive_name": "000000010000000000000001",
        "source_sha256": "a" * 64,
        "source_size": 16,
        "object_name": "ava-pitr/wal/00000001/000000010000000000000001.enc",
        "pin_token": "999",
        "ciphertext_size": 100,
        "ciphertext_crc32c": "walcrc",
        "ciphertext_checksum_algo": "crc32c",
        "ciphertext_checksum_value": "walcrc",
        "encryption_format": "AVAPITR1",
        "key_id": "v1",
        "acknowledged_at": "2026-08-30T10:00:00+00:00",
    }
    migrate._rewrite_ack(raw, _mapping())
    assert raw["pin_token"] == "200:wal-md5"  # noqa: S105 — fixture identity
    assert raw["ciphertext_checksum_algo"] == "md5"
    assert raw["ciphertext_checksum_value"] == "wal-md5"
    assert raw["ciphertext_crc32c"] == "walcrc"


# ── candidate rewrite ──


def test_rewrite_candidate_legacy_shape() -> None:
    raw: dict[str, Any] = {
        "schema_version": 1,
        "chain_id": "activation-20260830T043835Z-ab",
        "protected": False,
        "postgres_major": 17,
        "database_name": "ava_main",
        "system_identifier": "7656686487711429617",
        "wal_segment_size": 16777216,
        "timeline": 1,
        "start_lsn": "A4/7FC179B0",
        "end_lsn": "A4/89EC6820",
        "wal_ranges": [{"end_lsn": "A4/89EC6820", "start_lsn": "A4/7FC179B0", "timeline": 1}],
        "migration_set_sha256": "m" * 64,
        "base_object": {
            "object_name": "ava-pitr/base/20260830T043835Z/ab/base.tar.zst.enc",
            "generation": 1788085003231815,
            "ciphertext_size": 6319665156,
            "ciphertext_crc32c": "viqqbw==",
            "source_sha256": "sha",
            "source_size": 1,
            "key_id": "key",
            "encryption_format": "AVAPITRB1",
        },
        "native_manifest_sha256": "native",
        "native_manifest_member_path": "backup_manifest",
        "native_manifest_container_object_name": "ava-pitr/base/20260830T043835Z/ab/base.tar.zst.enc",
        "native_manifest_container_generation": 1788085003231815,
    }
    migrate._rewrite_candidate(raw, _mapping())
    base = raw["base_object"]
    assert base["pin_token"] == "100:basemd5"  # noqa: S105 — fixture identity
    assert base["ciphertext_checksum_algo"] == "md5"
    assert base["ciphertext_checksum_value"] == "basemd5"
    assert base["ciphertext_crc32c"] == "viqqbw=="
    assert "generation" not in base
    assert raw["native_manifest_container_pin_token"] == "100:basemd5"  # noqa: S105
    assert "native_manifest_container_generation" not in raw
    # the rewritten candidate parses in the new shape
    from services.pitr.base_manifest import CandidateManifest

    parsed = CandidateManifest.from_json(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    assert parsed.base_object.pin_token == "100:basemd5"  # noqa: S105


# ── protected rewrite ──


def test_rewrite_protected_swaps_base_wal_and_embedded_candidate() -> None:
    raw: dict[str, Any] = {
        "schema_version": 1,
        "protected": True,
        "chain_id": "activation-20260830T043835Z-ab",
        "candidate_sha256": "x",
        "candidate": {
            "schema_version": 1,
            "chain_id": "activation-20260830T043835Z-ab",
            "protected": False,
            "base_object": {
                "object_name": "ava-pitr/base/20260830T043835Z/ab/base.tar.zst.enc",
                "generation": 1788085003231815,
                "ciphertext_size": 6319665156,
                "ciphertext_crc32c": "viqqbw==",
                "source_sha256": "sha",
                "source_size": 1,
                "key_id": "key",
                "encryption_format": "AVAPITRB1",
            },
            "native_manifest_sha256": "native",
            "native_manifest_member_path": "backup_manifest",
            "native_manifest_container_object_name": "ava-pitr/base/20260830T043835Z/ab/base.tar.zst.enc",
            "native_manifest_container_generation": 1788085003231815,
        },
        "base": {
            "archive_name": "base.tar.zst.enc",
            "object_name": "ava-pitr/base/20260830T043835Z/ab/base.tar.zst.enc",
            "generation": 1788085003231815,
            "size": 6319665156,
            "crc32c": "viqqbw==",
            "metadata": [],
        },
        "wal": [
            {
                "archive_name": "000000010000000000000001",
                "object_name": "ava-pitr/wal/00000001/000000010000000000000001.enc",
                "generation": 999,
                "size": 16777216,
                "crc32c": "walcrc",
                "metadata": [],
            }
        ],
    }
    migrate._rewrite_protected(raw, _mapping())
    assert raw["base"]["pin_token"] == "100:basemd5"  # noqa: S105 — fixture identity
    assert raw["base"]["checksum_algo"] == "md5"
    assert raw["base"]["checksum_value"] == "basemd5"
    assert "generation" not in raw["base"] and "crc32c" not in raw["base"]
    wal = raw["wal"][0]
    assert wal["pin_token"] == "200:wal-md5"  # noqa: S105 — fixture identity
    assert wal["checksum_algo"] == "md5"
    assert wal["checksum_value"] == "wal-md5"
    assert "generation" not in wal and "crc32c" not in wal
    embedded = raw["candidate"]["base_object"]
    assert embedded["pin_token"] == "100:basemd5"  # noqa: S105 — fixture identity


# ── fail-closed + snapshot + preflight ──


def test_record_referencing_missing_object_fails_closed() -> None:
    raw: dict[str, Any] = {
        "archive_name": "000000010000000000000001",
        "object_name": "ava-pitr/wal/00000001/000000010000000000000009.enc",
        "pin_token": "1",
        "ciphertext_checksum_algo": "crc32c",
        "ciphertext_checksum_value": "x",
    }
    with pytest.raises(SystemExit, match="missing from the migration"):
        migrate._rewrite_ack(raw, _mapping())


def test_snapshot_tars_the_identity_records(tmp_path: Path) -> None:
    for name in migrate._RECORD_DIRS:
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.json").write_text("{}")
    (tmp_path / "operation.json").write_text('{"phase": "protected"}')
    snapshot = tmp_path / "snap.tar.gz"

    migrate._snapshot(tmp_path, snapshot)

    with tarfile.open(snapshot, "r:gz") as archive:
        members = {member.name for member in archive.getmembers()}
    assert "base-manifests/x.json" in members
    assert "ack/x.json" in members
    assert "operation.json" in members


def test_preflight_rejects_in_flight_activation(tmp_path: Path) -> None:
    (tmp_path / "operation.json").write_text('{"phase": "restore_pending"}')
    with pytest.raises(SystemExit, match="in-flight"):
        migrate._preflight(tmp_path)
    (tmp_path / "operation.json").write_text('{"phase": "protected"}')
    migrate._preflight(tmp_path)  # must not raise
    (tmp_path / "operation.json").unlink()
    migrate._preflight(tmp_path)  # must not raise
