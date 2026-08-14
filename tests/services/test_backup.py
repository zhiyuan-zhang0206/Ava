"""services/backup.py — due-ness, prune, foreign-file safety, failure cleanup,
and a real pg_dump round-trip against the session's provisioned Postgres."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import cast

import pytest

from services import backup


@pytest.fixture
def bdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(backup, "backup_dir", lambda: tmp_path)
    return tmp_path


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute).astimezone()  # aware local, like the module


def _touch(directory: Path, name: str) -> Path:
    p = directory / name
    p.write_bytes(b"x")
    return p


def test_not_due_before_backup_hour(bdir: Path) -> None:
    assert not backup.is_due(_dt(2026, 6, 10, backup.BACKUP_HOUR - 1, 59))


def test_due_at_hour_with_no_dumps(bdir: Path) -> None:
    assert backup.is_due(_dt(2026, 6, 10, backup.BACKUP_HOUR, 0))


def test_not_due_again_after_todays_dump(bdir: Path) -> None:
    _touch(bdir, "ava-20260610-030001.dump")
    assert not backup.is_due(_dt(2026, 6, 10, 9, 0))


def test_due_again_the_next_day(bdir: Path) -> None:
    _touch(bdir, "ava-20260609-030001.dump")
    assert backup.is_due(_dt(2026, 6, 10, backup.BACKUP_HOUR, 0))


def test_catchup_after_downtime(bdir: Path) -> None:
    """Host down at 03:00 -> the first tick later that day is still due."""
    _touch(bdir, "ava-20260609-030001.dump")
    assert backup.is_due(_dt(2026, 6, 10, 14, 30))


def test_foreign_files_ignored(bdir: Path) -> None:
    """Hand-made dumps / stray files are neither counted for due-ness nor pruned."""
    foreign = [
        _touch(bdir, "manual.dump"),
        _touch(bdir, "ava-before-migration.dump"),
        _touch(bdir, "notes.txt"),
    ]
    assert backup.is_due(_dt(2026, 6, 10, 9, 0))
    assert backup._prune(bdir) == []
    assert all(p.exists() for p in foreign)


def test_prune_keeps_newest(bdir: Path) -> None:
    names = [f"ava-202606{i:02d}-030000.dump" for i in range(1, 11)]  # 10 days, oldest first
    for name in names:
        _touch(bdir, name)
    removed = backup._prune(bdir)
    keep = backup.BACKUP_KEEP
    assert [p.name for p in removed] == names[: len(names) - keep]
    assert sorted(p.name for p in bdir.glob("*.dump")) == names[len(names) - keep :]


def test_run_backup_failure_leaves_no_files(bdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Failed:
        returncode = 1
        stderr = "connection refused"

    monkeypatch.setattr(backup.subprocess, "run", lambda *_a, **_kw: _Failed())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(RuntimeError, match="pg_dump exited 1"):
        backup.run_backup(_dt(2026, 6, 10, 3, 0), db_url="dbname=whatever")
    assert list(bdir.iterdir()) == []  # no .dump, no .partial


def test_run_backup_sweeps_stale_partials(bdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A `.partial` left by an interrupted run (e.g. the process tree killed
    mid-rollout) is swept before the new dump is written, so it cannot pile up
    unnoticed: the name never matches `_NAME_RE`, so due/prune logic ignores it."""
    (bdir / "whatever-20260801-030000.dump.partial").write_bytes(b"stale")
    (bdir / "other-20260801-031500.dump.partial").write_bytes(b"stale")

    class _Failed:
        returncode = 1
        stderr = "connection refused"

    monkeypatch.setattr(backup.subprocess, "run", lambda *_a, **_kw: _Failed())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(RuntimeError, match="pg_dump exited 1"):
        backup.run_backup(_dt(2026, 8, 2, 3, 0), db_url="dbname=whatever")
    assert not list(bdir.glob("*.partial"))  # both stale partials swept
    assert list(bdir.iterdir()) == []


@pytest.mark.skipif(not backup.pg_tool("pg_dump").exists(), reason="needs a native pg_dump binary")
def test_run_backup_real_dump_and_prune(bdir: Path) -> None:
    """Real pg_dump against the session's provisioned Postgres: dump lands under
    the managed name, the .partial intermediate is gone, and old dumps prune."""
    for i in range(1, backup.BACKUP_KEEP + 1):
        _touch(bdir, f"test-2026060{i}-030000.dump")

    path = backup.run_backup(_dt(2026, 6, 10, 3, 0))

    assert path.parent == bdir
    assert backup._NAME_RE.match(path.name)
    assert path.stat().st_size > 0
    assert not list(bdir.glob("*.partial"))
    # BACKUP_KEEP pre-seeded + 1 new -> the oldest pre-seed pruned, KEEP remain.
    assert len(backup._managed_dumps(bdir)) == backup.BACKUP_KEEP
    assert not (bdir / "test-20260601-030000.dump").exists()
    # The fresh dump is restorable input: pg_restore can list its TOC.
    import subprocess

    listing = subprocess.run(  # noqa: S603
        [str(backup.pg_tool("pg_restore")), "--list", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert listing.returncode == 0, listing.stderr


def test_run_backup_excludes_checkpoint_tables_and_uses_headroom_timeout(
    bdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daily dump excludes the LangGraph runtime tables (checkpoint_blobs
    alone outgrew the old 30-min ceiling and dead-looped the backup) and runs
    under the 60-min headroom timeout. #1035 incident pin."""
    captured: dict[str, list[str] | int] = {}

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _Ok:
        captured["cmd"] = cmd
        captured["timeout"] = cast(int, kwargs.get("timeout"))
        # the real pg_dump would write the partial; fake it so the rename lands
        file_idx = cmd.index("--file") + 1
        Path(cmd[file_idx]).write_bytes(b"x")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)
    backup.run_backup(_dt(2026, 8, 8, 3, 0), db_url="dbname=whatever")

    cmd = captured["cmd"]
    assert isinstance(cmd, list)
    exclusions = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--exclude-table"]
    assert exclusions == list(backup._EXCLUDE_TABLES)
    assert captured["timeout"] == backup._DUMP_TIMEOUT_S
