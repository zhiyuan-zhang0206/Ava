from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.pitr.activation_state import ActivationRecord, load_record, record_path, write_record


def test_activation_record_keeps_original_started_at_across_resume(tmp_path: Path) -> None:
    first = ActivationRecord.start(operation_id="op-1", origin="agent:405")
    write_record(tmp_path, first)
    resumed = first.advance("snapshot_verified", pre_activation_snapshot="/backup.enc")
    write_record(tmp_path, resumed)

    loaded = load_record(tmp_path)
    assert loaded is not None
    assert loaded.started_at == first.started_at
    assert loaded.updated_at >= first.updated_at
    assert loaded.phase == "snapshot_verified"
    assert record_path(tmp_path).stat().st_mode & 0o777 == 0o600


def test_activation_record_rejects_unknown_fields(tmp_path: Path) -> None:
    record = ActivationRecord.start(operation_id="op-1", origin="cli")
    write_record(tmp_path, record)
    path = record_path(tmp_path)
    raw = json.loads(path.read_text())
    raw["future"] = True
    path.write_text(json.dumps(raw))

    with pytest.raises(ValueError, match="fields differ"):
        load_record(tmp_path)


def test_activation_record_atomic_write_leaves_no_partial(tmp_path: Path) -> None:
    write_record(tmp_path, ActivationRecord.start(operation_id="op-1", origin="cli"))
    assert list(record_path(tmp_path).parent.glob(".operation-*.partial")) == []
