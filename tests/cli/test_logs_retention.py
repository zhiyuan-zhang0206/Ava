"""`ava logs retention` local-file safety and reporting contract."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def _stale_file(logs: Path, name: str, body: bytes) -> Path:
    path = logs / name
    path.write_bytes(body)
    stale = (_NOW - timedelta(days=15)).timestamp()
    os.utime(path, (stale, stale))
    return path


def _file_at(logs: Path, name: str, mtime: datetime) -> Path:
    path = logs / name
    path.write_text(name, encoding="utf-8")
    timestamp = mtime.timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


def test_dry_run_reports_all_managed_families_without_deleting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.logs import cmd_logs_retention

    managed = [
        _stale_file(tmp_path, "ava-agent-12.out.log", b"main"),
        _stale_file(tmp_path, "ava-agent-12-shell-3-review.out.log", b"shell"),
        _stale_file(tmp_path, "ava-agent-12-shell-3-review.host.log", b"host"),
        _stale_file(
            tmp_path,
            "agent-12.2026-08-01_00-00-00_12345.log",
            b"jsonl",
        ),
    ]

    rc = cmd_logs_retention(
        older_than_days=14,
        dry_run=True,
        logs_path=tmp_path,
        now=_NOW,
    )

    assert rc == 0
    assert all(path.exists() for path in managed)
    out = capsys.readouterr().out
    for path in managed:
        assert (
            "retention_candidate\tmtime=2026-08-09T12:00:00Z"
            f"\tsize_bytes={path.stat().st_size}\tpath={path}"
        ) in out
    assert "retention_summary\tmode=dry-run\tfiles=4\tbytes=18" in out


def test_active_open_file_is_excluded_from_retention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.logs import cmd_logs_retention

    active = _stale_file(tmp_path, "ava-agent-12.out.log", b"active")

    with active.open("rb"):
        rc = cmd_logs_retention(
            older_than_days=14,
            dry_run=False,
            logs_path=tmp_path,
            now=_NOW,
        )

    assert rc == 0
    assert active.exists()
    out = capsys.readouterr().out
    assert str(active) not in out
    assert "retention_summary\tmode=delete\tdeleted=0\tbytes=0\tfailed=0" in out


def test_delete_reports_reclaimed_bytes_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.logs import cmd_logs_retention

    expired = _stale_file(tmp_path, "ava-agent-12.out.log", b"gone")

    first_rc = cmd_logs_retention(
        older_than_days=14,
        dry_run=False,
        logs_path=tmp_path,
        now=_NOW,
    )

    assert first_rc == 0
    assert not expired.exists()
    assert "retention_summary\tmode=delete\tdeleted=1\tbytes=4\tfailed=0" in capsys.readouterr().out

    second_rc = cmd_logs_retention(
        older_than_days=14,
        dry_run=False,
        logs_path=tmp_path,
        now=_NOW,
    )

    assert second_rc == 0
    assert "retention_summary\tmode=delete\tdeleted=0\tbytes=0\tfailed=0" in capsys.readouterr().out


def test_delete_failure_is_reported_and_other_candidates_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.commands.logs import cmd_logs_retention

    failed = _stale_file(tmp_path, "ava-agent-12.out.log", b"bad")
    deleted = _stale_file(tmp_path, "ava-agent-13.out.log", b"good")
    real_unlink = Path.unlink

    def refuse_one(path: Path, *args: object, **kwargs: object) -> None:
        if path == failed:
            raise PermissionError("denied by test")
        real_unlink(path, *args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(Path, "unlink", refuse_one)

    rc = cmd_logs_retention(
        older_than_days=14,
        dry_run=False,
        logs_path=tmp_path,
        now=_NOW,
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert failed.exists()
    assert not deleted.exists()
    assert f"retention_error\tpath={failed}\terror=denied by test" in captured.err
    assert "retention_summary\tmode=delete\tdeleted=1\tbytes=4\tfailed=1" in captured.out


def test_unreadable_matching_path_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.commands import logs

    unreadable = tmp_path / "ava-agent-12.out.log"

    class UnreadableEntry:
        name = unreadable.name
        path = str(unreadable)

        def is_file(self, *, follow_symlinks: bool) -> bool:
            assert follow_symlinks is False
            return True

        def stat(self, *, follow_symlinks: bool) -> os.stat_result:
            assert follow_symlinks is False
            raise PermissionError("cannot stat")

    class EntryScan:
        def __enter__(self) -> list[UnreadableEntry]:
            return [UnreadableEntry()]

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_scandir(_path: Path) -> EntryScan:
        return EntryScan()

    monkeypatch.setattr(logs.os, "scandir", fake_scandir)

    rc = logs.cmd_logs_retention(
        older_than_days=14,
        dry_run=False,
        logs_path=tmp_path,
        now=_NOW,
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert f"retention_error\tpath={unreadable}\terror=cannot stat" in captured.err
    assert "retention_summary\tmode=delete\tdeleted=0\tbytes=0\tfailed=1" in captured.out


def test_retention_preserves_every_path_outside_the_exact_boundary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.logs import cmd_logs_retention

    logs = tmp_path / "logs"
    logs.mkdir()
    preserved = [
        _file_at(
            logs,
            "ava-agent-20.out.log",
            _NOW - timedelta(days=14),
        ),
        _file_at(
            logs,
            "restarter.2026-08-24_10-00-00_12345.log",
            _NOW - timedelta(hours=1),
        ),
        _stale_file(logs, "ava-agent-x.out.log", b"bad-id"),
        _stale_file(logs, "ava-agent-20.stderr.log", b"stderr"),
        _stale_file(logs, "ava-agent-20-shell-2.out.log", b"unnamed"),
        _stale_file(
            logs,
            "restarter.log.2026-08-01_00-00-00_12345.log",
            b"wrong-rotation-shape",
        ),
    ]
    nested = logs / "archive"
    nested.mkdir()
    nested_log = _stale_file(nested, "ava-agent-30.out.log", b"nested")
    symlink_target = _stale_file(tmp_path, "outside.log", b"outside")
    matching_symlink = logs / "ava-agent-31.out.log"
    matching_symlink.symlink_to(symlink_target)
    matching_directory = logs / "ava-agent-32.out.log"
    matching_directory.mkdir()

    rc = cmd_logs_retention(
        older_than_days=14,
        dry_run=False,
        logs_path=logs,
        now=_NOW,
    )

    assert rc == 0
    assert all(path.exists() for path in preserved)
    assert nested_log.exists()
    assert matching_symlink.is_symlink()
    assert symlink_target.exists()
    assert matching_directory.is_dir()
    assert "retention_summary\tmode=delete\tdeleted=0\tbytes=0\tfailed=0" in capsys.readouterr().out


def test_configured_days_apply_when_the_flag_is_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.commands.logs import cmd_logs_retention
    from shared.config import settings

    managed = _stale_file(tmp_path, "ava-agent-12.out.log", b"keep")
    monkeypatch.setattr(settings.observability, "log_retention_days", 16)

    rc = cmd_logs_retention(
        older_than_days=None,
        dry_run=False,
        logs_path=tmp_path,
        now=_NOW,
    )

    assert rc == 0
    assert managed.exists()
    assert "retention_summary\tmode=delete\tdeleted=0\tbytes=0\tfailed=0" in capsys.readouterr().out
