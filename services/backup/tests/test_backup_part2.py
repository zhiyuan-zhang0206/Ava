from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from base.config import settings
from base.db import Database
from services.backup import dump as backup
from services.backup.artifact import offsite

_CLUSTER_TZ = "America/Los_Angeles"


@pytest.fixture
def bdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pg_binaries = {name: backup.pg_tool(name) for name in ("pg_dump", "pg_restore")}
    monkeypatch.setattr(backup, "pg_tool", pg_binaries.__getitem__)
    monkeypatch.setattr(backup, "backup_dir", lambda: tmp_path)
    monkeypatch.setattr(settings.general, "timezone", _CLUSTER_TZ)
    return tmp_path


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(_CLUSTER_TZ))


def _touch(directory: Path, name: str) -> Path:
    p = directory / name
    p.write_bytes(b"x")
    return p


def _disable_offsite(monkeypatch: pytest.MonkeyPatch) -> None:

    def _skip(_artifact: Path, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(offsite, "publish", _skip)


def test_run_backup_avoids_overwriting_a_same_second_dump(
    bdir: Path, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    existing = _touch(bdir, "whatever-20260808T100000Z.dump.enc")

    class _Ok:
        returncode = 0
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _Ok:
        if cmd[0].endswith("pg_dump"):
            Path(cmd[cmd.index("--file") + 1]).write_bytes(b"plaintext dump")
        else:
            Path(cmd[cmd.index("-out") + 1]).write_bytes(b"encrypted dump")
        return _Ok()

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)

    created = backup.run_backup(
        _dt(2026, 8, 8, 3, 0),
        db_url="dbname=whatever",
        db=database,
        is_remote_reader=lambda: settings.data_plane.is_remote,
        keep_reader=lambda: settings.services.backup_keep,
        endpoint_reader=lambda: settings.services.backup_offsite_endpoint,
        bucket_reader=lambda: settings.services.backup_offsite_bucket,
        credentials_file_reader=lambda: settings.services.backup_offsite_credentials_file,
    )

    assert created.name == "whatever-20260808T100001Z.dump.enc"
    assert existing.read_bytes() == b"x"
    assert created.read_bytes() == b"encrypted dump"
