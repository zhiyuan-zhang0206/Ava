"""Record-rewrite tests for scripts/pitr_migrate_gcs_to_baidu.py.

The GCS -> Baidu migration rewrites local identity records field-level:
ACKs, candidate manifests, and protected manifests must swap their GCS
vocabulary (generation + crc32c) for Baidu pins (fs_id:md5) while keeping
every other byte of the original serialization — these tests pin both
shapes and the fail-closed path.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from services.pitr.checksums import MD5, ObjectChecksum
from services.pitr.object_store import RemoteObjectAck

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


# ── full re-parse (QA #1155) ──


def _full_protected_fixture() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "protected": True,
        "chain_id": "activation-20260830T043835Z-ab",
        "candidate_sha256": "x",
        "candidate": {
            "schema_version": 1,
            "chain_id": "activation-20260830T043835Z-ab",
            "protected": False,
            "postgres_major": 17,
            "database_name": "ava_main",
            "system_identifier": "7656686487711429617",
            "wal_segment_size": 16777216,
            "timeline": 1,
            "start_lsn": "0/00000000",
            "end_lsn": "0/01000000",
            "wal_ranges": [{"end_lsn": "0/01000000", "start_lsn": "0/00000000", "timeline": 1}],
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
                "archive_name": "000000010000000000000000",
                "object_name": "ava-pitr/wal/00000001/000000010000000000000000.enc",
                "generation": 777,
                "size": 16777216,
                "crc32c": "walcrc0",
                "metadata": [],
            }
        ],
        "target_lsn": "0/01000000",
        "wal_segment_size": 16777216,
        "proof": {
            "run_id": "run-1",
            "started_at": "2026-08-30T10:00:00+00:00",
            "completed_at": "2026-08-30T10:05:00+00:00",
            "target_lsn": "0/01000000",
            "achieved_lsn": "0/01000000",
            "live_postgres_pid": 1,
            "live_probe_sha256": "p" * 64,
            "candidate_verify_evidence_sha256": "c" * 64,
            "replay_seconds": 1.0,
            "smoke_seconds": 1.0,
            "restored_verify_seconds": 1.0,
            "downloaded_bytes": 100,
            "restored_fingerprint_sha256": "f" * 64,
        },
    }


def test_rewrite_protected_reparses_as_a_protected_manifest() -> None:
    """QA #1155: the rewritten manifest must load through the very parser the
    post-cut Baidu drill uses — the candidate digest is recomputed over the
    rewritten canonical bytes, so ProtectedManifest.from_json accepts it."""
    from services.pitr.restore_manifest import ProtectedManifest, candidate_sha256

    mapping = {
        **_mapping(),
        "ava-pitr/wal/00000001/000000010000000000000000.enc": {
            "object_name": "ava-pitr/wal/00000001/000000010000000000000000.enc",
            "gcs_generation": "777",
            "gcs_crc32c": "walcrc0",
            "baidu_pin": "300:wal0-md5",
            "size": "16777216",
            "md5": "wal0-md5",
        },
    }
    raw = _full_protected_fixture()
    migrate._rewrite_protected(raw, mapping)
    # GCS vocabulary is gone at every level (base / wal / embedded candidate)
    assert "generation" not in raw["base"] and "crc32c" not in raw["base"]
    assert "generation" not in raw["wal"][0] and "crc32c" not in raw["wal"][0]
    embedded_base = raw["candidate"]["base_object"]
    assert "generation" not in embedded_base
    assert embedded_base["ciphertext_checksum_algo"] == "md5"
    assert "native_manifest_container_generation" not in raw["candidate"]

    parsed = ProtectedManifest.from_json(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    assert parsed.base.pin_token == "100:basemd5"  # noqa: S105 — fixture identity
    assert parsed.wal[0].pin_token == "300:wal0-md5"  # noqa: S105 — fixture identity
    assert parsed.candidate.base_object.pin_token == "100:basemd5"  # noqa: S105
    # the digest now pins the rewritten canonical candidate bytes
    assert raw["candidate_sha256"] == candidate_sha256(parsed.candidate)


# ── object copy (QA #1155 fold-in) ──


class _FakeGcsBlob:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.name = "ava-pitr/wal/00000001/000000010000000000000000.enc"
        self.generation = 123
        self.crc32c = ""  # skipped GCS-side verify
        self.size = len(payload)

    def download_to_filename(self, path: str) -> None:
        Path(path).write_bytes(self._payload)


class _FakeBaiduStore:
    def __init__(self, checksum_value: str) -> None:
        self._checksum_value = checksum_value

    def put_wal_ciphertext_if_absent(
        self, path: Path, object_name: str, metadata: Mapping[str, str]
    ) -> RemoteObjectAck:
        return RemoteObjectAck(
            object_name=object_name,
            pin_token="300:wal0-md5",  # noqa: S106 — fixture identity
            size=path.stat().st_size,
            checksum=ObjectChecksum(MD5, self._checksum_value),
            metadata=dict(metadata),
            created=True,
        )


def test_migrate_object_returns_the_verified_mapping_row() -> None:
    payload = b"wal-bytes" * 64
    row = migrate._migrate_object(
        baidu=_FakeBaiduStore(hashlib.md5(payload).hexdigest()),  # noqa: S324
        blob=_FakeGcsBlob(payload),
        object_name="ava-pitr/wal/00000001/000000010000000000000000.0000000000000000.enc",
        size=len(payload),
        metadata={"ava-speedtest": "1"},
    )
    assert row["baidu_pin"] == "300:wal0-md5"
    assert row["md5"] == hashlib.md5(payload).hexdigest()  # noqa: S324
    assert row["gcs_generation"] == "123"
    assert row["size"] == str(len(payload))


def test_migrate_object_aborts_when_the_baidu_read_back_differs() -> None:
    payload = b"wal-bytes" * 64
    with pytest.raises(SystemExit, match="read-back differs"):
        migrate._migrate_object(
            baidu=_FakeBaiduStore("not-the-md5"),
            blob=_FakeGcsBlob(payload),
            object_name="ava-pitr/wal/00000001/000000010000000000000000.0000000000000000.enc",
            size=len(payload),
            metadata={},
        )


# ── QA #1155 nits: two-phase plan + GCS crc32c branch ──


def test_rewrite_records_writes_nothing_when_planning_fails(tmp_path: Path) -> None:
    """QA #1155 nit 1: the two-phase plan is the zero-write guarantee — a
    record that fails planning (missing mapping object) must abort with
    every other record byte-identical."""
    ack_dir = tmp_path / "ack"
    ack_dir.mkdir()
    good = ack_dir / "000000010000000000000001.ack.json"
    good.write_text(
        json.dumps(
            {
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
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    bad = ack_dir / "000000010000000000000002.ack.json"
    bad.write_text(
        json.dumps(
            {
                "archive_name": "000000010000000000000002",
                "object_name": "ava-pitr/wal/00000001/000000010000000000000009.enc",
                "pin_token": "1",
                "ciphertext_checksum_algo": "crc32c",
                "ciphertext_checksum_value": "x",
            }
        )
    )
    before = {path: path.read_bytes() for path in (good, bad)}

    with pytest.raises(SystemExit, match="missing from the migration"):
        migrate._rewrite_records(tmp_path, _mapping(), dry_run=False)

    assert {path: path.read_bytes() for path in (good, bad)} == before


def test_migrate_object_verifies_the_gcs_crc32c_before_upload(tmp_path: Path) -> None:
    """QA #1155 nit 2: the GCS-side crc32c gate — a blob whose declared
    crc32c differs from the downloaded bytes must abort before the Baidu
    upload."""
    payload = b"wal-bytes" * 64
    payload_path = tmp_path / "payload.bin"
    payload_path.write_bytes(payload)
    other_path = tmp_path / "other.bin"
    other_path.write_bytes(b"different-bytes")

    blob = _FakeGcsBlob(payload)
    blob.crc32c = migrate._crc32c(other_path)
    with pytest.raises(SystemExit, match="crc32c mismatch"):
        migrate._migrate_object(
            baidu=_FakeBaiduStore(hashlib.md5(payload).hexdigest()),  # noqa: S324
            blob=blob,
            object_name="ava-pitr/wal/00000001/000000010000000000000000.0000000000000000.enc",
            size=len(payload),
            metadata={},
        )
    blob.crc32c = migrate._crc32c(payload_path)
    migrate._migrate_object(
        baidu=_FakeBaiduStore(hashlib.md5(payload).hexdigest()),  # noqa: S324
        blob=blob,
        object_name="ava-pitr/wal/00000001/000000010000000000000000.0000000000000000.enc",
        size=len(payload),
        metadata={},
    )
