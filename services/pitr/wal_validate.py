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
        _validate_history(path)
        return
    timeline, segment_number = _segment_identity(path.name, candidate.wal_segment_size)
    if path.stat().st_size != candidate.wal_segment_size:
        raise ValueError("WAL segment size differs from the candidate")
    segment_start = segment_number * candidate.wal_segment_size
    with path.open("rb") as source:
        for block_offset in range(0, candidate.wal_segment_size, _WAL_BLOCK_BYTES):
            header = source.read(40)
            if len(header) != 40:
                raise ValueError("WAL page header is truncated")
            magic, info, page_timeline = struct.unpack_from("<HHI", header, 0)
            page_address = struct.unpack_from("<Q", header, 8)[0]
            if magic == 0 and not any(header):
                source.seek(_WAL_BLOCK_BYTES - 40, 1)
                continue
            if magic == 0 or page_timeline != timeline:
                raise ValueError("WAL page magic or timeline differs from its filename")
            if page_address != segment_start + block_offset:
                raise ValueError("WAL page address differs from its segment position")
            if block_offset == 0:
                if not info & _XLP_LONG_HEADER:
                    raise ValueError("first WAL page omitted its long header")
                system_identifier = struct.unpack_from("<Q", header, 24)[0]
                segment_size, block_size = struct.unpack_from("<II", header, 32)
                if (
                    str(system_identifier) != candidate.system_identifier
                    or segment_size != candidate.wal_segment_size
                    or block_size != _WAL_BLOCK_BYTES
                ):
                    raise ValueError("WAL long header differs from the candidate")
            source.seek(_WAL_BLOCK_BYTES - 40, 1)


def _validate_history(path: Path) -> None:
    timeline = int(path.name.removesuffix(".history"), 16)
    rows = [line for line in path.read_text().splitlines() if line and not line.startswith("#")]
    if not rows:
        raise ValueError("timeline history is empty")
    parents: list[int] = []
    for row in rows:
        fields = row.split("\t")
        if len(fields) < 2:
            raise ValueError("timeline history row is malformed")
        parents.append(int(fields[0], 16))
        high, low = fields[1].split("/", 1)
        int(high, 16), int(low, 16)
    if parents != sorted(set(parents)) or parents[-1] >= timeline:
        raise ValueError("timeline history ancestry is not canonical")
