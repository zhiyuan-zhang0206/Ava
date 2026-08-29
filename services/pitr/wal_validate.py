"""Validate restored PostgreSQL 17 WAL before recovery can consume it."""

from __future__ import annotations

import struct
from pathlib import Path

from services.pitr.base_manifest import CandidateManifest

_XLP_LONG_HEADER = 0x0002
_WAL_BLOCK_BYTES = 8192


def _segment_identity(name: str, segment_size: int) -> tuple[int, int]:
    if len(name) != 24:
        raise ValueError("WAL segment name must contain 24 hexadecimal digits")
    timeline = int(name[:8], 16)
    log = int(name[8:16], 16)
    offset = int(name[16:], 16)
    segments_per_log = 0x100000000 // segment_size
    if offset >= segments_per_log:
        raise ValueError("WAL segment offset exceeds the configured segment size")
    return timeline, log * segments_per_log + offset


def validate_wal_file(path: Path, candidate: CandidateManifest) -> None:
    """Bind page headers to filename, system id, timeline and segment size."""

    if path.name.endswith(".history"):
        _validate_history(path, candidate)
        return
    timeline, segment_number = _segment_identity(path.name, candidate.wal_segment_size)
    if path.stat().st_size != candidate.wal_segment_size:
        raise ValueError("WAL segment size differs from the candidate")
    segment_start = segment_number * candidate.wal_segment_size
    with path.open("rb") as source:
        for block_offset in range(0, candidate.wal_segment_size, _WAL_BLOCK_BYTES):
            page = source.read(_WAL_BLOCK_BYTES)
            if len(page) != _WAL_BLOCK_BYTES:
                raise ValueError("WAL page header is truncated")
            magic, info, page_timeline = struct.unpack_from("<HHI", page, 0)
            page_address = struct.unpack_from("<Q", page, 8)[0]
            if not any(page):
                continue
            if magic == 0 or page_timeline != timeline:
                raise ValueError("WAL page magic or timeline differs from its filename")
            if page_address != segment_start + block_offset:
                raise ValueError("WAL page address differs from its segment position")
            if block_offset == 0:
                if not info & _XLP_LONG_HEADER:
                    raise ValueError("first WAL page omitted its long header")
                system_identifier = struct.unpack_from("<Q", page, 24)[0]
                segment_size, block_size = struct.unpack_from("<II", page, 32)
                if (
                    str(system_identifier) != candidate.system_identifier
                    or segment_size != candidate.wal_segment_size
                    or block_size != _WAL_BLOCK_BYTES
                ):
                    raise ValueError("WAL long header differs from the candidate")


def _validate_history(path: Path, candidate: CandidateManifest) -> None:
    timeline = int(path.name.removesuffix(".history"), 16)
    transitions = [
        (previous.timeline, current.timeline, current.start_lsn)
        for previous, current in zip(candidate.wal_ranges, candidate.wal_ranges[1:], strict=False)
        if current.timeline == timeline
    ]
    first_range = candidate.wal_ranges[0]
    starts_on_timeline = timeline == first_range.timeline and timeline > 1
    if len(transitions) != 1 and not starts_on_timeline:
        raise ValueError("timeline history is not required by the candidate")
    rows = [line for line in path.read_text().splitlines() if line and not line.startswith("#")]
    if not rows:
        raise ValueError("timeline history is empty")
    parents: list[tuple[int, str]] = []
    for row in rows:
        fields = row.split("\t")
        if len(fields) < 2:
            raise ValueError("timeline history row is malformed")
        parent = int(fields[0], 10)
        high, low = fields[1].split("/", 1)
        _position = (int(high, 16), int(low, 16))
        parents.append((parent, fields[1]))
    expected_parent = transitions[0][0] if transitions else parents[-1][0]
    expected_switch = transitions[0][2] if transitions else parents[-1][1]
    if (
        [item[0] for item in parents] != sorted({item[0] for item in parents})
        or parents[-1][0] >= timeline
        or parents[-1] != (expected_parent, expected_switch)
        or (
            starts_on_timeline
            and _lsn_position(expected_switch) > _lsn_position(first_range.start_lsn)
        )
    ):
        raise ValueError("timeline history ancestry is not canonical")


def _lsn_position(value: str) -> int:
    high, low = value.split("/", 1)
    return (int(high, 16) << 32) | int(low, 16)
