"""`ava backup operations status|retire` over the scheduled backup operation kinds.

The commands read the real kinds under `$AVA_HOME/backups/`, so each test lays
a control directory out literally in a tmp home, as a stopped controller leaves
it, and reads what an operator sees.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.native_process import native_boot_id
from base.native_process.os_platform import file_lock
from cli.commands.data_plane.backup_operations import (
    cmd_backup_operations_retire,
    cmd_backup_operations_status,
)

_DUMP_CONTROLS = ("backups", "operations", "dump")
_DRILL_CONTROLS = ("backups", "operations", "restore-drill")


def _stopped_controller(home: Path, controls: tuple[str, ...], name: str, *, boot_id: str) -> Path:
    """A control directory whose controller died after recording only its launch."""
    work = home.joinpath(*controls, name)
    work.mkdir(parents=True)
    (work / "operation.json").write_text(
        json.dumps(
            {
                "kind": "logical-dump",
                "module": "services.backup.scheduler.worker",
                "boot_id": boot_id,
                "at": "2026-09-30T19:00:00+00:00",
            }
        )
    )
    return work


def test_status_on_a_fresh_home_is_ready_everywhere(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cmd_backup_operations_status() == 0

    out = capsys.readouterr().out
    assert f"logical-dump: ready ({unit_home.joinpath(*_DUMP_CONTROLS)})" in out
    assert f"logical-restore-drill: ready ({unit_home.joinpath(*_DRILL_CONTROLS)})" in out
    assert "BLOCKED" not in out and "ava backup operations retire" not in out


def test_status_names_the_blocked_operation_and_the_way_out(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _stopped_controller(unit_home, _DRILL_CONTROLS, ".operation-x1", boot_id=native_boot_id() or "")

    assert cmd_backup_operations_status() == 1

    out = capsys.readouterr().out
    assert "logical-dump: ready" in out
    assert "logical-restore-drill: BLOCKED" in out
    assert ".operation-x1: the controller stopped before proving closure" in out
    assert "run `ava backup operations retire`" in out


def test_retire_previews_then_quarantines_a_proven_operation(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work = _stopped_controller(
        unit_home, _DUMP_CONTROLS, ".operation-x2", boot_id="an-earlier-boot"
    )
    quarantine = unit_home / "backups" / "quarantine" / "logical-dump"

    assert cmd_backup_operations_retire(confirm=False) == 0
    preview = capsys.readouterr().out
    assert "closure proven" in preview and "preview only" in preview and work.is_dir()

    assert cmd_backup_operations_retire(confirm=True) == 0
    done = capsys.readouterr().out
    assert "retired into" in done and not work.exists()
    assert len(list(quarantine.iterdir())) == 1

    assert cmd_backup_operations_status() == 0
    assert "quarantine" in capsys.readouterr().out
    assert cmd_backup_operations_retire(confirm=True) == 0
    assert "no blocked operations" in capsys.readouterr().out


def test_retire_refuses_what_it_cannot_prove(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work = _stopped_controller(
        unit_home, _DUMP_CONTROLS, ".operation-x3", boot_id=native_boot_id() or ""
    )

    assert cmd_backup_operations_retire(confirm=True) == 1

    captured = capsys.readouterr()
    assert "closure NOT proven [launch-unrecorded]" in captured.err
    assert work.is_dir()


def test_retire_says_so_while_an_operation_is_running(
    unit_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = unit_home.joinpath(*_DUMP_CONTROLS)
    root.mkdir(parents=True)

    with file_lock(root / ".lock", timeout_s=0):
        assert cmd_backup_operations_retire(confirm=True) == 1

    assert "logical-dump: an operation is running; retry once it settles" in capsys.readouterr().err
