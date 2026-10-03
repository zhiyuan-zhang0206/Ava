"""Backup intermediates: an interrupted dump leaves no plaintext, and every run sweeps closed ones."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from base.db import Database
from services import backup


def test_interrupted_snapshot_dump_leaves_no_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    """A dump interrupted mid-run (stop, Ctrl-C, timeout) kills and reaps
    its writer before the plaintext partial is removed."""
    monkeypatch.setattr(backup, "backup_dir", lambda: tmp_path)

    def composition(_db: Database, _db_url: str | None = None) -> str:
        return "test"

    monkeypatch.setattr(backup, "_db_size_breakdown", composition)
    writer = (
        "import sys,time\n"
        "open(sys.argv[sys.argv.index('--file')+1],'wb').write(b'PLAINTEXT')\n"
        "time.sleep(60)\n"
    )

    def dump_argv(_tool: str) -> Path:
        script = tmp_path / "pg_dump"
        script.write_text(f"#!{sys.executable}\n{writer}")
        script.chmod(0o700)
        return script

    monkeypatch.setattr(backup, "pg_tool", dump_argv)

    def interrupt(line: str) -> None:
        if "0s" in line or line.endswith("MiB written"):
            raise KeyboardInterrupt

    monkeypatch.setattr(backup, "_PROGRESS_INTERVAL_S", 0.2)
    with pytest.raises(KeyboardInterrupt):
        backup.run_backup(db_url="dbname=ava", publish=False, progress=interrupt, db=database)
    assert not list(tmp_path.glob("*.partial")) and not list(tmp_path.glob(".backup-key-*"))


def test_stale_partial_waits_for_its_orphaned_writer_to_close(tmp_path: Path) -> None:
    """A killed run's orphaned tool may still hold its partial: it stays until
    that writer exits, then the next run removes it."""
    from services.gateway_side.backup.intermediates import sweep_closed_partials

    held = tmp_path / "ava-20260926T000000Z.dump.partial"
    closed = tmp_path / "ava-20260925T000000Z.dump.enc.partial"
    key = tmp_path / ".backup-key-abc"
    for path in (held, closed, key):
        path.write_bytes(b"PLAINTEXT")
    writer = subprocess.Popen(  # noqa: S603 -- disposable orphan stand-in
        [
            sys.executable,
            "-c",
            "import sys,time;f=open(sys.argv[1],'ab');print('open',flush=True);time.sleep(60)",
            str(held),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout is not None and writer.stdout.readline() == "open\n"
        sweep_closed_partials(tmp_path)
        assert held.exists() and not closed.exists() and not key.exists()
    finally:
        writer.kill()
        writer.wait(timeout=10)
    sweep_closed_partials(tmp_path)
    assert not held.exists()


def test_every_backup_run_sweeps_closed_intermediates_from_the_backup_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: Database
) -> None:
    """A killed run's plaintext partial never outlives the next run: the
    scheduled (staged) worker run sweeps the backup directory too, and the same
    sweep removes key files and an abandoned cross-filesystem copy."""
    from datetime import UTC, datetime

    published = tmp_path / "db"
    published.mkdir(mode=0o700)
    monkeypatch.setattr(backup, "backup_dir", lambda: published)

    def composition(_db: Database, _db_url: str | None = None) -> str:
        return "test"

    monkeypatch.setattr(backup, "_db_size_breakdown", composition)
    script = tmp_path / "pg_dump"
    script.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "open(sys.argv[sys.argv.index('--file')+1],'wb').write(b'DUMP')\n"
    )
    script.chmod(0o700)

    def dump_tool(_tool: str) -> Path:
        return script

    monkeypatch.setattr(backup, "pg_tool", dump_tool)
    stale = [
        published / "ava-20260920T030000Z.dump.partial",
        published / ".backup-key-stale",
        published / ".ava-20260925T030000Z.dump.enc.k3y9.copy",
    ]
    for path in stale:
        path.write_bytes(b"PLAINTEXT")
    staging = tmp_path / "controls" / "artifact"
    artifact = backup.run_backup(
        datetime(2026, 9, 26, 3, tzinfo=UTC),
        db_url="dbname=ava",
        publish=False,
        staging=staging,
        db=database,
    )
    assert artifact.parent == staging
    assert [path.name for path in stale if path.exists()] == []
    assert list(published.iterdir()) == []
