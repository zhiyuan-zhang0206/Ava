from __future__ import annotations

import struct
from dataclasses import replace
from pathlib import Path

import pytest

from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
from services.pitr.wal_validate import validate_wal_file


def _candidate() -> CandidateManifest:
    return CandidateManifest(
        schema_version=1,
        chain_id="20260829T000000Z",
        protected=False,
        postgres_major=17,
        database_name="ava",
        system_identifier="42",
        wal_segment_size=16 * 1024 * 1024,
        timeline=1,
        start_lsn="0/1000000",
        end_lsn="0/2000000",
        wal_ranges=(WalRange(1, "0/1000000", "0/2000000"),),
        base_object=BaseObject("base", "1", 1, "crc32c", "crc", "sha", 1, "key", "AVAPITRB1"),
        native_manifest_sha256="native",
        native_manifest_member_path="backup_manifest",
        native_manifest_container_object_name="base",
        native_manifest_container_pin_token="1",  # noqa: S106 — test fixture
        migration_set_sha256="migrations",
    )


def _wal(path: Path, *, system_identifier: int = 42, timeline: int = 1) -> None:
    value = bytearray(16 * 1024 * 1024)
    struct.pack_into("<HHI", value, 0, 0xD119, 0x0002, timeline)
    struct.pack_into("<Q", value, 8, 16 * 1024 * 1024)
    struct.pack_into("<QII", value, 24, system_identifier, len(value), 8192)
    path.write_bytes(value)


def test_wal_header_binds_system_timeline_segment_and_filename(tmp_path: Path) -> None:
    path = tmp_path / "000000010000000000000001"
    _wal(path)
    validate_wal_file(path, _candidate())

    _wal(path, system_identifier=43)
    with pytest.raises(ValueError, match="long header"):
        validate_wal_file(path, _candidate())


def test_timeline_history_requires_canonical_ancestry(tmp_path: Path) -> None:
    candidate = replace(
        _candidate(),
        end_lsn="0/3000000",
        wal_ranges=(
            WalRange(1, "0/1000000", "0/2000000"),
            WalRange(2, "0/2000000", "0/3000000"),
        ),
    )
    path = tmp_path / "00000002.history"
    path.write_text("1\t0/2000000\tparent\n")
    validate_wal_file(path, candidate)

    path.write_text("2\t0/2000000\tself\n")
    with pytest.raises(ValueError, match="ancestry"):
        validate_wal_file(path, candidate)


def test_zero_header_with_nonzero_payload_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "000000010000000000000001"
    value = bytearray(16 * 1024 * 1024)
    value[40] = 1
    path.write_bytes(value)
    with pytest.raises(ValueError, match="magic"):
        validate_wal_file(path, _candidate())


def test_first_range_on_later_timeline_accepts_ancestry_before_start(tmp_path: Path) -> None:
    candidate = replace(
        _candidate(),
        timeline=2,
        start_lsn="0/2000000",
        end_lsn="0/3000000",
        wal_ranges=(WalRange(2, "0/2000000", "0/3000000"),),
    )
    path = tmp_path / "00000002.history"
    path.write_text("1\t0/1800000\tparent\n")
    validate_wal_file(path, candidate)

    path.write_text("1\t0/2800000\tfuture\n")
    with pytest.raises(ValueError, match="ancestry"):
        validate_wal_file(path, candidate)
