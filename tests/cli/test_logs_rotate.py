"""`ava logs rotate` copytruncate and path-boundary contract."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def _dated_file(path: Path, body: bytes, mtime: datetime) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    timestamp = mtime.timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


def _archive(path: Path) -> Path:
    return Path(f"{path}.2026-09-07")


def test_dry_run_reports_rotation_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.logs import cmd_logs_rotate

    log = _dated_file(tmp_path / "ava-gateway.out.log", b"gateway", _NOW - timedelta(days=1))

    rc = cmd_logs_rotate(dry_run=True, size_mib=64, logs_path=tmp_path, now=_NOW)

    assert rc == 0
    assert log.read_bytes() == b"gateway"
    assert not _archive(log).exists()
    assert f"rotate_state\tpath={log}\tbytes=7\taction=rotated" in capsys.readouterr().out


def test_copytruncate_preserves_path_and_inode_and_archives_content(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.logs import cmd_logs_rotate

    log = _dated_file(
        tmp_path / "ava-otel-collector.out.log", b"retry spam", _NOW - timedelta(days=1)
    )
    inode = log.stat().st_ino

    rc = cmd_logs_rotate(dry_run=False, size_mib=64, logs_path=tmp_path, now=_NOW)

    assert rc == 0
    assert log.exists()
    assert log.stat().st_ino == inode
    assert log.read_bytes() == b""
    assert _archive(log).read_bytes() == b"retry spam"
    assert f"rotate_state\tpath={log}\tbytes=10\taction=rotated" in capsys.readouterr().out


def test_existing_daily_archive_makes_rotation_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.logs import cmd_logs_rotate

    log = _dated_file(tmp_path / "ava-gateway.out.log", b"live", _NOW - timedelta(days=1))
    archive = _dated_file(_archive(log), b"first", _NOW)

    rc = cmd_logs_rotate(dry_run=False, size_mib=64, logs_path=tmp_path, now=_NOW)

    assert rc == 0
    assert log.read_bytes() == b"live"
    assert archive.read_bytes() == b"first"
    assert f"rotate_state\tpath={log}\tbytes=0\taction=kept" in capsys.readouterr().out


def test_size_threshold_rotates_a_same_day_file(tmp_path: Path) -> None:
    from cli.commands.logs import cmd_logs_rotate

    log = _dated_file(tmp_path / "ava-gateway.out.log", b"", _NOW)
    with log.open("r+b") as stream:
        stream.truncate(1 << 20)
    os.utime(log, (_NOW.timestamp(), _NOW.timestamp()))

    assert cmd_logs_rotate(dry_run=False, size_mib=1, logs_path=tmp_path, now=_NOW) == 0
    assert log.stat().st_size == 0
    assert _archive(log).stat().st_size == 1 << 20


def test_utc_day_change_rotates_a_small_file(tmp_path: Path) -> None:
    from cli.commands.logs import cmd_logs_rotate

    log = _dated_file(
        tmp_path / "ava-ops.out.log",
        b"x",
        datetime(2026, 9, 6, 23, 59, tzinfo=UTC),
    )

    assert cmd_logs_rotate(dry_run=False, size_mib=64, logs_path=tmp_path, now=_NOW) == 0
    assert log.read_bytes() == b""
    assert _archive(log).read_bytes() == b"x"


def test_prior_utc_day_rotates_even_an_empty_file(tmp_path: Path) -> None:
    from cli.commands.logs import cmd_logs_rotate

    log = _dated_file(tmp_path / "ava-ops.out.log", b"", _NOW - timedelta(days=1))

    assert cmd_logs_rotate(dry_run=False, size_mib=64, logs_path=tmp_path, now=_NOW) == 0
    assert _archive(log).exists()
    assert _archive(log).read_bytes() == b""


def test_native_scope_excludes_grafana_archives_symlinks_and_nested_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.logs import cmd_logs_rotate

    logs_path = tmp_path / "logs"
    native = tmp_path / "lgtm" / "native" / "logs"
    logs_path.mkdir()
    native.mkdir(parents=True)
    loki = _dated_file(native / "loki.log", b"loki", _NOW - timedelta(days=1))
    grafana = _dated_file(native / "grafana.log", b"grafana", _NOW - timedelta(days=1))
    prior_archive = _dated_file(
        native / "prometheus.log.2026-09-06", b"prior", _NOW - timedelta(days=1)
    )
    nested = _dated_file(native / "nested" / "dbg-stdout.log", b"nested", _NOW - timedelta(days=1))
    outside = _dated_file(tmp_path / "outside.log", b"outside", _NOW - timedelta(days=1))
    symlink = native / "prometheus.log"
    symlink.symlink_to(outside)

    rc = cmd_logs_rotate(dry_run=False, size_mib=64, logs_path=logs_path, now=_NOW)

    assert rc == 0
    assert loki.read_bytes() == b""
    assert _archive(loki).read_bytes() == b"loki"
    assert grafana.read_bytes() == b"grafana"
    assert prior_archive.read_bytes() == b"prior"
    assert nested.read_bytes() == b"nested"
    assert symlink.is_symlink()
    assert outside.read_bytes() == b"outside"
    out = capsys.readouterr().out
    assert str(loki) in out
    assert str(grafana) not in out
    assert str(prior_archive) not in out
    assert str(nested) not in out
    assert str(symlink) not in out


def test_logs_path_flag_reaches_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import commands
    from cli import main as cli_main

    seen: list[dict[str, object]] = []
    monkeypatch.setattr(commands, "cmd_logs_rotate", lambda **kwargs: seen.append(kwargs) or 0)

    rc = cli_main.main(
        ["logs", "rotate", "--dry-run", "--size-mib", "8", "--logs-path", str(tmp_path)]
    )

    assert rc == 0
    assert seen == [{"dry_run": True, "size_mib": 8, "logs_path": tmp_path}]


def test_io_error_is_reported_and_sets_failure_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.commands import logs

    log = _dated_file(tmp_path / "ava-gateway.out.log", b"gateway", _NOW - timedelta(days=1))
    monkeypatch.setattr(
        logs.shutil,
        "copyfile",
        lambda _source, _archive: (_ for _ in ()).throw(PermissionError("denied")),
    )

    rc = logs.cmd_logs_rotate(dry_run=False, size_mib=64, logs_path=tmp_path, now=_NOW)

    captured = capsys.readouterr()
    assert rc == 1
    assert log.read_bytes() == b"gateway"
    assert f"rotate_error\tpath={log}\terror=denied" in captured.err
