"""Pruning the quarantine after a move never turns a kept entry into a failed one."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.native_process import native_boot_id
from services.backup.scheduler.operation import custody


def test_pruning_after_the_move_never_reports_a_failed_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_prune(*_args: object) -> None:
        raise OSError("quarantine root is read-only")

    monkeypatch.setattr(custody, "_prune_quarantine", broken_prune)
    work = tmp_path / "controls" / ".operation-closed"
    work.mkdir(parents=True)
    (work / "operation.json").write_text(
        json.dumps({"kind": "test", "module": "m", "boot_id": native_boot_id(), "at": "t"})
    )
    (work / "closure.json").write_text("{}")
    kind = custody.OperationKind("test", tmp_path / "controls", tmp_path / "quarantine")
    entry = custody.quarantine(kind, work, "failure")
    assert entry.is_dir() and not work.exists()
    assert not (entry / custody.QUARANTINE_FAILED).exists()
