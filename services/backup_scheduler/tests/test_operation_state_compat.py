"""Operation controls an earlier release left on disk are read and settled unchanged.

A rollout can stop a controller mid-operation, so the next release finds
`.operation-*` control directories under `$AVA_HOME/backups/operations/<kind>/`.
Every name here is the on-disk contract: the control and quarantine roots, the
kind names, the record file names and the directory prefixes. Each directory is
laid out literally, never through the code under test, so renaming any of them
fails these tests instead of stranding or mis-settling a live control.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.native_process import native_boot_id
from base.native_process.os_platform import file_lock
from services.backup_scheduler.operation import custody
from services.backup_scheduler.operation.worker_process import run_operation
from services.backup_scheduler.worker import dump_kind, restore_drill_kind

_AT = "2026-09-30T19:00:00+00:00"
_ARTIFACT = "ava-20260930T190000Z.dump.enc"


def _control(root: Path, name: str, **records: object) -> Path:
    """One control directory holding exactly the named record files."""
    work = root / name
    work.mkdir(parents=True)
    for stem, body in records.items():
        (work / f"{stem}.json").write_text(json.dumps(body))
    return work


def _operation(kind: str, *, boot_id: str | None) -> dict[str, object]:
    return {
        "kind": kind,
        "module": "services.backup_scheduler.worker",
        "boot_id": boot_id,
        "at": _AT,
    }


def test_the_kinds_keep_their_on_disk_names(unit_home: Path) -> None:
    dump, drill = dump_kind(), restore_drill_kind()

    assert (dump.name, dump.control_root, dump.quarantine_root) == (
        "logical-dump",
        unit_home / "backups" / "operations" / "dump",
        unit_home / "backups" / "quarantine" / "logical-dump",
    )
    assert (drill.name, drill.control_root, drill.quarantine_root) == (
        "logical-restore-drill",
        unit_home / "backups" / "operations" / "restore-drill",
        unit_home / "backups" / "quarantine" / "logical-restore-drill",
    )


def test_each_custody_state_is_read_as_the_earlier_release_wrote_it(unit_home: Path) -> None:
    root = dump_kind().control_root
    boot = native_boot_id()
    _control(root, ".operation-unresolved", operation=_operation("logical-dump", boot_id=boot))
    (root / ".operation-unresolved" / "unresolved.json").write_text('{"at": "x", "error": "e"}')
    _control(root, ".operation-no-closure", operation=_operation("logical-dump", boot_id=boot))
    _control(root, ".operation-closed", closure={"proven_by": "controller", "at": _AT})
    _control(root, ".operation-committed", committed={"at": _AT})
    failed = _control(root, ".operation-quarantine-failed", closure={"proven_by": "c", "at": _AT})
    (failed / "quarantine-failed.json").write_text('{"at": "x", "error": "disk full"}')
    (root / ".retired-leftover").mkdir()
    (root / "stray").mkdir()
    (root / ".lock").write_text("")

    blocked = {path.name: reason for path, reason in custody.blocked_operations(dump_kind())}

    assert set(blocked) == {
        ".operation-unresolved",
        ".operation-no-closure",
        ".operation-quarantine-failed",
        "stray",
    }
    assert "closure was unresolved" in blocked[".operation-unresolved"]
    assert blocked[".operation-no-closure"] == "the controller stopped before proving closure"
    assert "quarantine failed: disk full" in blocked[".operation-quarantine-failed"]
    assert blocked["stray"] == "unexpected entry in the control root"


def test_admission_settles_proven_leftovers_and_keeps_complete_artifacts(unit_home: Path) -> None:
    kind = dump_kind()
    closed = _control(
        kind.control_root, ".operation-closed", closure={"proven_by": "controller", "at": _AT}
    )
    (closed / "artifact").mkdir()
    (closed / "artifact" / _ARTIFACT).write_bytes(b"complete encrypted artifact")
    (closed / "artifact" / "ava-20260930T190000Z.dump.partial").write_bytes(b"PLAINTEXT")
    (closed / "artifact" / "ava-20260930T190000Z.dump.enc.partial").write_bytes(b"half")
    committed = _control(kind.control_root, ".operation-committed", committed={"at": _AT})
    (kind.control_root / ".retired-torn").mkdir()

    custody.admit(kind)

    assert not closed.exists() and not committed.exists()
    assert not (kind.control_root / ".retired-torn").exists()
    [entry] = custody.quarantine_entries(kind.quarantine_root)
    assert entry.name.endswith("-logical-dump-closed")
    assert (entry / "artifact" / _ARTIFACT).read_bytes() == b"complete encrypted artifact"
    assert sorted(path.name for path in (entry / "artifact").iterdir()) == [_ARTIFACT]
    assert "controller stopped after proving closure" in (entry / "failure.txt").read_text()


def test_admission_refuses_an_unproven_control_and_alerts(unit_home: Path) -> None:
    kind = restore_drill_kind()
    stopped = _control(
        kind.control_root,
        ".operation-stopped",
        operation=_operation("logical-restore-drill", boot_id=native_boot_id()),
    )

    with pytest.raises(custody.OperationBlockedError, match="ava backup operations retire"):
        custody.admit(kind)

    assert stopped.is_dir(), "an unproven control is never deleted"


async def test_a_blocked_kind_refuses_a_new_operation_before_launching(unit_home: Path) -> None:
    kind = dump_kind()
    _control(
        kind.control_root,
        ".operation-stopped",
        operation=_operation("logical-dump", boot_id=native_boot_id()),
    )

    with pytest.raises(custody.OperationBlockedError):
        await run_operation(
            "services.backup_scheduler.worker", {}, kind=kind, env={"PATH": "/usr/bin:/bin"}
        )

    assert [p.name for p in kind.control_root.glob(".operation-*")] == [".operation-stopped"]


async def test_a_held_kind_lock_is_busy_not_blocked(unit_home: Path) -> None:
    """The lock file keeps its name and place: a scheduler and an operator exclude each other."""
    kind = dump_kind()
    kind.control_root.mkdir(parents=True)

    with (
        file_lock(kind.control_root / ".lock", timeout_s=0),
        pytest.raises(
            custody.OperationBusyError, match="logical-dump operations are held elsewhere"
        ),
    ):
        await run_operation("services.backup_scheduler.worker", {}, kind=kind, env={})


def test_retirement_proves_a_rebooted_operation_and_quarantines_its_artifact(
    unit_home: Path,
) -> None:
    kind = dump_kind()
    work = _control(
        kind.control_root,
        ".operation-rebooted",
        operation=_operation("logical-dump", boot_id="a-boot-that-is-over"),
    )
    (work / "artifact").mkdir()
    (work / "artifact" / _ARTIFACT).write_bytes(b"complete")
    (work / "artifact" / "ava-20260930T190000Z.dump.partial").write_bytes(b"PLAINTEXT")

    [preview] = custody.retire_blocked(kind, confirm=False)
    assert preview.proven and preview.entry is None and work.is_dir()
    [retired] = custody.retire_blocked(kind, confirm=True)

    assert retired.proven and retired.refusal is None and not work.exists()
    assert retired.entry is not None and retired.entry.parent == kind.quarantine_root
    assert (retired.entry / "closure.json").is_file()
    assert [p.name for p in (retired.entry / "artifact").iterdir()] == [_ARTIFACT]
    assert custody.blocked_operations(kind) == []


def test_retirement_refuses_a_stopped_launch_it_cannot_prove(unit_home: Path) -> None:
    kind = dump_kind()
    work = _control(
        kind.control_root,
        ".operation-launching",
        operation=_operation("logical-dump", boot_id=native_boot_id()),
    )

    [report] = custody.retire_blocked(kind, confirm=True)

    assert not report.proven and report.refusal is custody.Refusal.LAUNCH_UNRECORDED
    assert work.is_dir()
