"""Backup cases: run with progress without a sink defers."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from base.config import settings
from base.db import Database
from base.native_process.os_platform import LockTimeoutError
from services.backup import dump as backup
from services.backup.artifact import offsite
from services.backup.tests.test_backup import (
    _await_backup_lock_holder,
    _disable_offsite,
    _dt,
    _spawn_backup_lock_holder,
)
from services.backup.tests.test_backup import (
    bdir as bdir,
)


def test_run_with_progress_without_a_sink_defers_to_subprocess_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`progress=None` is the untouched subprocess.run path the scheduler uses."""
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        _ = kwargs
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(backup.subprocess, "run", _fake_run)

    result = backup._run_with_progress(
        ["pg_dump", "--version"], timeout_s=5.0, label="pg_dump", progress=None
    )

    assert result.returncode == 0
    assert calls == [["pg_dump", "--version"]]


def test_run_with_progress_heartbeats_while_a_slow_child_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stage allowed to run for minutes narrates itself: one start line, then a
    line per interval carrying the bytes written. This is what keeps a slow
    dump from reading as a stalled rollout (2026-09-14 incident)."""
    monkeypatch.setattr(backup, "_PROGRESS_INTERVAL_S", 0.2)
    out = tmp_path / "slow.dump.partial"
    out.write_bytes(b"")
    stage = tmp_path / "slow_stage.py"
    stage.write_text(
        "import sys, time\n"
        "out = sys.argv[1]\n"
        "for _ in range(30):\n"
        "    with open(out, 'ab') as fh:\n"
        "        fh.write(b'x' * 65536)\n"
        "    time.sleep(0.06)\n",
        encoding="utf-8",
    )
    lines: list[str] = []

    proc = backup._run_with_progress(
        [sys.executable, str(stage), str(out)],
        timeout_s=120.0,
        label="pg_dump",
        progress=lines.append,
        size_path=out,
    )

    assert proc.returncode == 0
    assert lines[0] == "pg_dump started (bounded at 2 min)"
    beats = lines[1:]
    assert len(beats) >= 3, f"expected a beat per interval, got {lines}"
    assert all(beat.startswith("pg_dump ") and " MiB written" in beat for beat in beats)
    sizes = [float(beat.split(", ")[1].split(" ")[0]) for beat in beats]
    assert sizes == sorted(sizes), f"byte evidence must only grow: {sizes}"
    assert sizes[-1] > 0


def test_run_with_progress_timeout_still_kills_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The progress path keeps subprocess.run's bound: expiry kills the child and
    raises TimeoutExpired, so the scheduler's retry discipline is unchanged."""
    monkeypatch.setattr(backup, "_PROGRESS_INTERVAL_S", 0.2)
    marker = tmp_path / "finished"
    stage = tmp_path / "hung_stage.py"
    stage.write_text(
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(30)\n"
        f"Path({str(marker)!r}).write_text('finished')\n",
        encoding="utf-8",
    )
    lines: list[str] = []
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        backup._run_with_progress(
            [sys.executable, str(stage)],
            timeout_s=1.0,
            label="pg_dump",
            progress=lines.append,
        )

    assert time.monotonic() - started < 10
    assert lines[0].startswith("pg_dump started")
    assert not marker.exists()
    time.sleep(0.3)
    assert not marker.exists(), "the killed child must never complete its work"


def test_run_backup_narrates_both_silent_stages_through_progress(
    bdir: Path, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    """`run_backup(progress=...)` reports both stages allowed to run for minutes
    without output — pg_dump and the encryption pass — each with its label, its
    bound, and the file it writes."""
    stages: list[tuple[str, Path | None]] = []

    def _fake_stage(
        argv: list[str],
        *,
        timeout_s: float,
        label: str,
        progress: Callable[[str], None] | None,
        env: dict[str, str] | None = None,
        size_path: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        assert progress is not None, "run_backup must forward the caller's sink"
        _ = env
        assert timeout_s == backup._DUMP_TIMEOUT_S
        stages.append((label, size_path))
        progress(f"{label} started (bounded at {timeout_s / 60:.0f} min)")
        if size_path is not None:
            size_path.write_bytes(b"stage output")
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(backup, "_run_with_progress", _fake_stage)
    _disable_offsite(monkeypatch)
    lines: list[str] = []

    path = backup.run_backup(
        _dt(2026, 9, 14, 21, 0),
        db_url="dbname=whatever",
        progress=lines.append,
        db=database,
        is_remote_reader=lambda: settings.data_plane.is_remote,
        keep_reader=lambda: settings.services.backup_keep,
        endpoint_reader=lambda: settings.services.backup_offsite_endpoint,
        bucket_reader=lambda: settings.services.backup_offsite_bucket,
        credentials_file_reader=lambda: settings.services.backup_offsite_credentials_file,
    )

    assert path.exists()
    assert [label for label, _ in stages] == ["pg_dump", "backup encryption"]
    assert stages[0][1] is not None and stages[0][1].name.endswith(".dump.partial")
    assert stages[1][1] is not None and stages[1][1].name.endswith(".dump.enc.partial")
    minutes = backup._DUMP_TIMEOUT_S / 60
    assert lines == [
        f"pg_dump started (bounded at {minutes:.0f} min)",
        f"backup encryption started (bounded at {minutes:.0f} min)",
    ]


def test_backup_lock_reentrant_same_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A snapshot may take the lock before `run_backup` takes it again."""
    ava_home = tmp_path / "ava-home"
    monkeypatch.setenv("AVA_HOME", str(ava_home))

    with backup.backup_lock(), backup.backup_lock(timeout_s=0.5):
        pass

    with backup.backup_lock(timeout_s=0.5):
        pass


def test_backup_lock_cross_process_excludes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduler dump waits for a rollout snapshot already holding the lock."""
    ava_home = tmp_path / "ava-home"
    monkeypatch.setenv("AVA_HOME", str(ava_home))
    ready = tmp_path / "ready"
    holder = _spawn_backup_lock_holder(ava_home, ready, hold_s=2.0)
    try:
        _await_backup_lock_holder(ready, holder)

        started = time.monotonic()
        with backup.backup_lock(timeout_s=30):
            waited = time.monotonic() - started
        assert waited >= 0.4, (
            f"took the backup lock while another process held it (waited {waited:.2f}s)"
        )
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)


def test_backup_lock_timeout_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged snapshot produces a bounded failure rather than an unbounded wait."""
    ava_home = tmp_path / "ava-home"
    monkeypatch.setenv("AVA_HOME", str(ava_home))
    ready = tmp_path / "ready"
    holder = _spawn_backup_lock_holder(ava_home, ready, hold_s=30.0)
    try:
        _await_backup_lock_holder(ready, holder)

        started = time.monotonic()
        with pytest.raises(LockTimeoutError), backup.backup_lock(timeout_s=0.5):
            pytest.fail("took a backup lock another process was holding")
        waited = time.monotonic() - started
        assert 0.3 <= waited < 5
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=10)


def test_run_backup_serializes_dump_creation(
    bdir: Path, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    """The public entry point holds the cross-process lock around its body."""
    events: list[str] = []
    artifact = bdir / "verified.dump.enc"

    @contextmanager
    def _backup_lock(**_kwargs: object):
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    def _run_backup(_now: datetime | None = None, **_kwargs: object) -> Path:
        events.append("backup-body")
        artifact.write_bytes(b"encrypted")
        return artifact

    monkeypatch.setattr(backup, "backup_lock", _backup_lock)
    monkeypatch.setattr(backup, "_run_backup", _run_backup)

    def _record(name: str, result: object = None) -> Callable[[Path], object]:
        def record(_path: Path, **_kwargs: object) -> object:
            events.append(name)
            return result

        return record

    monkeypatch.setattr(offsite, "publish", _record("publish"))
    monkeypatch.setattr(backup, "_prune", _record("prune", []))

    assert (
        backup.run_backup(
            _dt(2026, 8, 8, 3, 0),
            db_url="dbname=whatever",
            db=database,
            is_remote_reader=lambda: settings.data_plane.is_remote,
            keep_reader=lambda: settings.services.backup_keep,
            endpoint_reader=lambda: settings.services.backup_offsite_endpoint,
            bucket_reader=lambda: settings.services.backup_offsite_bucket,
            credentials_file_reader=lambda: settings.services.backup_offsite_credentials_file,
        )
        == artifact
    )
    assert events == ["lock-enter", "backup-body", "publish", "prune", "lock-exit"]
