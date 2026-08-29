from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.pitr.restore_wal_command import restore


def _mapping(path: Path, source: Path) -> None:
    payload = {
        source.name: {
            "path": str(source),
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "size": source.stat().st_size,
        }
    }
    path.write_text(json.dumps(payload))


def test_restore_command_accepts_only_allowlisted_exact_plaintext(tmp_path: Path) -> None:
    source = tmp_path / "000000010000000000000001"
    source.write_bytes(b"wal")
    mapping = tmp_path / "mapping.json"
    _mapping(mapping, source)
    destination = tmp_path / "pg-wal" / source.name

    restore(mapping, source.name, destination)

    assert destination.read_bytes() == b"wal"


def test_restore_command_rejects_unknown_and_changed_sources(tmp_path: Path) -> None:
    source = tmp_path / "000000010000000000000001"
    source.write_bytes(b"wal")
    mapping = tmp_path / "mapping.json"
    _mapping(mapping, source)
    source.write_bytes(b"changed")

    with pytest.raises(ValueError, match="differs"):
        restore(mapping, source.name, tmp_path / "changed")
    with pytest.raises(ValueError, match="absent"):
        restore(mapping, "000000010000000000000002", tmp_path / "unknown")
    with pytest.raises(ValueError, match="basename"):
        restore(mapping, "../escape", tmp_path / "escape")
