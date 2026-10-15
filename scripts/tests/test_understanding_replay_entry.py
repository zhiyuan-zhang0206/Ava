"""The preview replay entry owns its captured database admission gate."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from base.db.code_version_gate import ProcessDbGate
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from scripts.verify import understanding_replay as replay


def test_report_uses_one_captured_gated_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture = Mock(return_value=LoadedCommit(tmp_path, "captured-before-move"))
    count = Mock(return_value=7)
    database = Mock()
    create = Mock(return_value=database)
    report = Mock()
    monkeypatch.setattr(replay, "require_preview", Mock())
    monkeypatch.setattr(replay, "ConfigBoot", Mock(return_value=Mock()))
    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    monkeypatch.setattr(replay, "process_name", lambda: "preview")
    monkeypatch.setattr(replay.Database, "from_settings", create)
    monkeypatch.setattr(replay, "print_replay_report", report)
    replay.main(["report", "42"])
    capture.assert_called_once_with()
    create.assert_called_once()
    report.assert_called_once_with(database, 42)
    count.assert_not_called()
    gate = create.call_args.kwargs["gate"]
    assert isinstance(gate, ProcessDbGate)
    assert gate.application_name() == "ava:preview:v7"
    count.assert_called_once_with(tmp_path, "captured-before-move")
    gate.observe_minimum(7)
    assert not gate.min_read_due()


def test_preview_refusal_precedes_capture_and_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture = Mock()
    create = Mock()
    monkeypatch.setattr(replay, "DOCKERENV", tmp_path / "missing")
    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(replay.Database, "from_settings", create)
    with pytest.raises(SystemExit, match="preview container only"):
        replay.main(["report", "42"])
    capture.assert_not_called()
    create.assert_not_called()
