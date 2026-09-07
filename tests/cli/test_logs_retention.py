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


def test_family_days_apply_tier_defaults_and_report_each_candidate_family(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.logs import cmd_logs_retention

    expired = [
        _file_at(tmp_path, "ava-agent-12.out.log", _NOW - timedelta(days=16)),
        _file_at(
            tmp_path,
            "ava-agent-12-shell-3-review.out.log",
            _NOW - timedelta(days=8),
        ),
        _file_at(
            tmp_path,
            "gateway.2026-08-01_00-00-00_12345.log",
            _NOW - timedelta(days=31),
        ),
        _file_at(
            tmp_path,
            "ops.2026-08-01_00-00-00_12345.log",
            _NOW - timedelta(days=31),
        ),
        _file_at(
            tmp_path,
            "delivery_watchdog.2026-08-01_00-00-00_12345.log",
            _NOW - timedelta(days=31),
        ),
        _file_at(
            tmp_path,
            "restarter.2026-08-01_00-00-00_12345.log",
            _NOW - timedelta(days=4),
        ),
    ]
    retained = [
        _file_at(tmp_path, "ava-agent-13.out.log", _NOW - timedelta(days=15)),
        _file_at(
            tmp_path,
            "ava-agent-13-shell-3-review.out.log",
            _NOW - timedelta(days=7),
        ),
        _file_at(
            tmp_path,
            "gateway.2026-08-01_00-00-00_54321.log",
            _NOW - timedelta(days=30),
        ),
        _file_at(
            tmp_path,
            "restarter.2026-08-01_00-00-00_54321.log",
            _NOW - timedelta(days=3),
        ),
    ]

    rc = cmd_logs_retention(
        older_than_days=None,
        family_days={},
        dry_run=True,
        logs_path=tmp_path,
        now=_NOW,
    )

    assert rc == 0
    assert all(path.exists() for path in expired + retained)
    out = capsys.readouterr().out
    for family, days in {
        "agent": 15,
        "shell": 7,
        "gateway": 30,
        "ops": 30,
        "watchdog": 30,
        "other": 3,
    }.items():
        assert f"retention_family\tfamily={family}\tdays={days}\tfiles=1\tbytes=" in out
    assert "retention_summary\tmode=dry-run\tfiles=6\tbytes=" in out


def test_family_days_override_a_specific_family(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.logs import cmd_logs_retention

    gateway = _file_at(
        tmp_path,
        "gateway.2026-08-01_00-00-00_12345.log",
        _NOW - timedelta(days=16),
    )

    rc = cmd_logs_retention(
        older_than_days=None,
        family_days={"gateway": 15},
        dry_run=True,
        logs_path=tmp_path,
        now=_NOW,
    )

    assert rc == 0
    assert gateway.exists()
    out = capsys.readouterr().out
    assert "retention_candidate\tfamily=gateway\tdays=15" in out
    assert f"\tpath={gateway}" in out
    assert "retention_family\tfamily=gateway\tdays=15\tfiles=1\tbytes=" in out


def test_family_days_dry_run_reports_empty_policy_families(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.commands.logs import cmd_logs_retention

    rc = cmd_logs_retention(
        older_than_days=None,
        family_days={},
        dry_run=True,
        logs_path=tmp_path,
        now=_NOW,
    )

    assert rc == 0
    out = capsys.readouterr().out
    for family, days in {
        "agent": 15,
        "shell": 7,
        "gateway": 30,
        "ops": 30,
        "watchdog": 30,
        "other": 3,
    }.items():
        assert f"retention_family\tfamily={family}\tdays={days}\tfiles=0\tbytes=0" in out


def test_older_than_and_family_days_cannot_be_combined(tmp_path: Path) -> None:
    from cli.commands.logs import cmd_logs_retention

    with pytest.raises(ValueError, match="mutually exclusive"):
        cmd_logs_retention(
            older_than_days=14,
            family_days={"agent": 15},
            dry_run=True,
            logs_path=tmp_path,
            now=_NOW,
        )


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
            older_than_days=None,
            family_days={},
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
        _stale_file(logs, "ava-Agent-x.out.log", b"bad-service-name"),
        _stale_file(logs, "ava-agent-20.stderr.log", b"stderr"),
        _stale_file(logs, "ava-agent-20-shell-2.stderr.log", b"stderr"),
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
        family_days=None,
        dry_run=False,
        logs_path=tmp_path,
        now=_NOW,
    )

    assert rc == 0
    assert managed.exists()
    assert "retention_summary\tmode=delete\tdeleted=0\tbytes=0\tfailed=0" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("name", "family"),
    [
        ("ava-gateway.out.log", "gateway"),
        ("ava-ops.out.log", "ops"),
        ("ava-agent-runner-watchdog.out.log", "watchdog"),
        ("ava-otel-collector.out.log", "other"),
        ("ava-agent-host.out.log", "agent"),
    ],
)
def test_service_stdout_names_are_managed_and_follow_service_family_rules(
    name: str, family: str
) -> None:
    from cli.commands.logs import _MANAGED_LOG_NAME, _log_family

    match = _MANAGED_LOG_NAME.fullmatch(name)
    assert match is not None
    assert match["svcout"] == name
    assert _log_family(match) == family


def test_agent_and_shell_names_keep_precedence_over_service_stdout() -> None:
    from cli.commands.logs import _MANAGED_LOG_NAME

    agent = _MANAGED_LOG_NAME.fullmatch("ava-agent-12.out.log")
    shell = _MANAGED_LOG_NAME.fullmatch("ava-agent-12-shell-3-review.out.log")

    assert agent is not None and agent["agent"] is not None and agent["svcout"] is None
    assert shell is not None and shell["shell"] is not None and shell["svcout"] is None


@pytest.mark.parametrize(
    ("name", "group", "family"),
    [
        ("loki.log.2026-09-07", "rotlog", "other"),
        ("prometheus.log.2026-09-07", "rotlog", "other"),
        ("dbg-stdout.log.2026-09-07", "rotlog", "other"),
        ("ava-gateway.out.log.2026-09-07", "rotout", "gateway"),
        ("ava-otel-collector.out.log.2026-09-07", "rotout", "other"),
    ],
)
def test_copytruncate_archives_are_managed(name: str, group: str, family: str) -> None:
    from cli.commands.logs import _MANAGED_LOG_NAME, _log_family

    match = _MANAGED_LOG_NAME.fullmatch(name)
    assert match is not None
    assert match[group] == name
    assert _log_family(match) == family


def test_active_service_stdout_is_excluded_via_open_path_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.commands import logs

    active = _file_at(tmp_path, "ava-gateway.out.log", _NOW - timedelta(days=31))

    def active_paths(_path: Path) -> set[Path]:
        return {active.resolve()}

    monkeypatch.setattr(logs, "_active_log_paths", active_paths)

    rc = logs.cmd_logs_retention(
        older_than_days=None,
        family_days={},
        dry_run=False,
        logs_path=tmp_path,
        now=_NOW,
    )

    assert rc == 0
    assert active.exists()
    assert str(active) not in capsys.readouterr().out


def test_retention_deletes_expired_service_stdout_and_native_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.commands import logs

    logs_path = tmp_path / "logs"
    native_path = tmp_path / "lgtm" / "native" / "logs"
    logs_path.mkdir()
    native_path.mkdir(parents=True)
    service = _file_at(logs_path, "ava-otel-collector.out.log", _NOW - timedelta(days=4))
    native_archive = _file_at(native_path, "loki.log.2026-08-01", _NOW - timedelta(days=4))
    live_native = _file_at(native_path, "loki.log", _NOW - timedelta(days=40))

    def no_active_paths(_path: Path) -> set[Path]:
        return set()

    monkeypatch.setattr(logs, "_active_log_paths", no_active_paths)

    rc = logs.cmd_logs_retention(
        older_than_days=None,
        family_days={},
        dry_run=False,
        logs_path=logs_path,
        now=_NOW,
    )

    assert rc == 0
    assert not service.exists()
    assert not native_archive.exists()
    assert live_native.exists()
    assert "retention_summary\tmode=delete\tdeleted=2" in capsys.readouterr().out
