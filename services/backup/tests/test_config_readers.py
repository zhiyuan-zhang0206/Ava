"""Backup operations consume required readers at their existing decision points."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.clock import Clock, ClockConfig
from base.config import ConfigBoot
from services.backup import dump
from services.backup.artifact import offsite
from services.backup.walg import config as walg_config


def test_offsite_preserves_live_read_order_and_repeated_fields(tmp_path: Path) -> None:
    calls: list[str] = []
    endpoints = iter(["https://first", "https://latest"])
    buckets = iter(["first", "latest"])
    credential_paths = iter([tmp_path / "first", tmp_path / "latest"])

    def endpoint() -> str:
        calls.append("endpoint")
        return next(endpoints)

    def bucket() -> str:
        calls.append("bucket")
        return next(buckets)

    def credentials() -> Path:
        calls.append("credentials")
        return next(credential_paths)

    assert offsite.configured_target(
        endpoint_reader=endpoint, bucket_reader=bucket, credentials_file_reader=credentials
    ) == offsite.OssTarget("https://latest", "latest", tmp_path / "latest")
    assert calls == ["endpoint", "bucket", "credentials", "credentials", "endpoint", "bucket"]


def test_due_reads_two_fresh_clocks_around_hour_and_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    zones = iter(["UTC", "America/Los_Angeles"])

    def clock() -> Clock:
        calls.append("clock")
        return Clock(ClockConfig(next(zones), None, False))

    def hour() -> int:
        calls.append("hour")
        return 3

    def dumps(_directory: Path) -> list[tuple[datetime, Path]]:
        calls.append("discovery")
        return [(datetime(2026, 1, 1, 2, tzinfo=UTC), Path("existing"))]

    monkeypatch.setattr(dump, "_managed_dumps", dumps)
    assert dump.is_due(datetime(2026, 1, 1, 4, tzinfo=UTC), clock_factory=clock, hour_reader=hour)
    assert calls == ["clock", "hour", "discovery", "clock"]


def test_retention_reads_current_keep_after_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    artifacts = [tmp_path / str(i) for i in range(3)]
    for artifact in artifacts:
        artifact.write_bytes(b"encrypted")

    def discover(_directory: Path) -> list[tuple[datetime, Path]]:
        calls.append("discovery")
        return [(datetime(2026, 1, i + 1, tzinfo=UTC), p) for i, p in enumerate(artifacts)]

    def keep() -> int:
        calls.append("keep")
        return 2

    monkeypatch.setattr(dump, "_managed_dumps", discover)
    assert dump._prune(tmp_path, keep_reader=keep) == artifacts[:1]
    assert calls == ["discovery", "keep"]
    assert [p.exists() for p in artifacts] == [False, True, True]


def test_walg_readers_follow_their_own_live_config_boot(tmp_path: Path) -> None:
    first, second = ConfigBoot(), ConfigBoot()
    first.set_field("walg_config_file", tmp_path / "first")
    second.set_field("walg_config_file", tmp_path / "second")

    def a() -> Path | None:
        return first.view.walg.walg_config_file

    def b() -> Path | None:
        return second.view.walg.walg_config_file

    assert walg_config.configured_path(path_reader=a) == tmp_path / "first"
    assert walg_config.configured_path(path_reader=b) == tmp_path / "second"
    first.set_field("walg_config_file", tmp_path / "updated")
    assert walg_config.configured_path(path_reader=a) == tmp_path / "updated"
    assert walg_config.configured_path(path_reader=b) == tmp_path / "second"
    first.set_field("walg_config_file", None)
    assert not walg_config.enabled(path_reader=a)
    assert walg_config.enabled(path_reader=b)
