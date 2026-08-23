"""Session-code provenance stays attached to one process identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from shared import session_code
from shared.session_record import SessionRecord


def _write_session_record(root: Path, *, pid: int, started_at: float) -> None:
    SessionRecord(
        pid=pid,
        create_time=started_at,
        cmd="daemon",
        cwd="/repo",
        started_at=started_at,
        starttime=pid,
    ).write(root / "sessions" / "ava-ops.json")


def test_launch_sha_does_not_survive_a_reused_session_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A healthcheck respawn with the same name must not inherit old code facts."""
    monkeypatch.setattr(session_code, "run_dir", lambda: tmp_path)
    _write_session_record(tmp_path, pid=10, started_at=1.0)

    session_code.record_launch("ava-ops", "oldsha")
    assert session_code.launched_sha("ava-ops") == "oldsha"

    _write_session_record(tmp_path, pid=11, started_at=2.0)
    assert session_code.launched_sha("ava-ops") is None
