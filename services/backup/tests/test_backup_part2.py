"""services/backup/dump.py — due-ness, prune, foreign-file safety, failure cleanup,
and a real pg_dump round-trip against the session's provisioned Postgres.

Every clock here is pinned: the module decides *when* in cluster time
(`AVA_TIMEZONE`) and *names* dumps in UTC, so a test that let either fall back
to the host's timezone would pass or fail by which machine ran it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from base.config import settings
from base.db import Database
from services.backup import dump as backup
from services.backup.artifact import offsite

_CLUSTER_TZ = "America/Los_Angeles"
_REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def bdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(backup, "backup_dir", lambda: tmp_path)
    monkeypatch.setattr(settings.general, "timezone", _CLUSTER_TZ)
    return tmp_path


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """A cluster wall-clock instant — the clock is_due() reads."""
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(_CLUSTER_TZ))


def _touch(directory: Path, name: str) -> Path:
    p = directory / name
    p.write_bytes(b"x")
    return p


def _disable_offsite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the off-site leg in tests that only exercise the local pipeline."""

    def _skip(_artifact: Path, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(offsite, "publish", _skip)


# Literal source with data in argv, so test selection can read its imports.
# argv: the repo root, the ready file, then the hold seconds.
_BACKUP_LOCK_HOLDER = """
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from services.backup.dump import backup_lock

with backup_lock(timeout_s=60):
    Path(sys.argv[2]).write_text("1", encoding="utf-8")
    time.sleep(float(sys.argv[3]))
"""


def _spawn_backup_lock_holder(
    ava_home: Path, ready: Path, hold_s: float
) -> subprocess.Popen[bytes]:
    """A separate interpreter that takes `backup_lock`, signals, and holds it."""
    env = dict(os.environ)
    env["AVA_HOME"] = str(ava_home)
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _BACKUP_LOCK_HOLDER, str(_REPO), str(ready), str(hold_s)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )


def _await_backup_lock_holder(ready: Path, proc: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert proc.poll() is None, "the holder exited before taking the backup lock"
        assert time.monotonic() < deadline, "the holder never took the backup lock"
        time.sleep(0.02)


# ─── the clock the schedule is read on ───


# ─── the clock dumps are named on ───


def test_run_backup_avoids_overwriting_a_same_second_dump(
    bdir: Path, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    """A second managed writer publishes a new encrypted name, not a replacement."""
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

    created = backup.run_backup(_dt(2026, 8, 8, 3, 0), db_url="dbname=whatever", db=database)

    assert created.name == "whatever-20260808T100001Z.dump.enc"
    assert existing.read_bytes() == b"x"
    assert created.read_bytes() == b"encrypted dump"
