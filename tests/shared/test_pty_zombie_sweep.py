"""A zombie cannot execute; uncertain or live shells retain their records."""

import os
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest

from shared.pty_sessions import cli
from shared.pty_sessions._paths import record_path, socket_path
from shared.session_record import SessionRecord


@pytest.mark.parametrize("starttime", [None, 123])
@pytest.mark.parametrize("state", ["zombie", "running", "unreadable"])
def test_lazy_sweep_requires_proven_shell_exit(
    unit_home: Path, monkeypatch: pytest.MonkeyPatch, starttime: int | None, state: str
) -> None:
    """List hides stale observations but deletes artifacts only with exit proof."""
    name = "ava-test-zombie-sweep"
    record = SessionRecord(
        pid=os.getpid(),
        create_time=1.0,
        cmd="test",
        cwd=str(unit_home),
        started_at=1.0,
        starttime=starttime,
    )
    record.write(record_path(name))
    socket_path(name).touch()
    proc = Mock()
    proc.is_running.return_value = True
    proc.create_time.return_value = 1.0
    if state == "unreadable":
        proc.status.side_effect = psutil.AccessDenied(record.pid)
    else:
        proc.status.return_value = (
            psutil.STATUS_ZOMBIE if state == "zombie" else psutil.STATUS_RUNNING
        )
    monkeypatch.setattr(cli.psutil, "Process", lambda _pid: proc)
    monkeypatch.setattr(SessionRecord, "identifies", lambda _self, _pid: True)

    listed = cli.live_sessions(prefix=name)
    assert bool(listed) is (state == "running")
    assert record_path(name).exists() is (state != "zombie"), "zombie record must be swept"
    assert socket_path(name).exists() is (state != "zombie")
