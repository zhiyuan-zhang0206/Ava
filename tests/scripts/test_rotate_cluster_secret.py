"""Human-bearer rotation pins the logical-backup passphrase before it rotates.

Logical backups are encrypted under a passphrase derived from the gateway's
cluster secret until it first rotates; the rotation pins that passphrase so
artifacts written before and after it share one key. Every step is journaled
before its effect, with fingerprints only, and a resumed rotation never
re-derives the passphrase from the new secret.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from scripts import rotate_cluster_secret as rotate
from services import backup
from services.gateway_side.backup import passphrase
from shared.config import settings

_OLD = "old-bearer-" + "o" * 32
_RESTORES = itertools.count()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A gateway home whose `.env` holds `_OLD`, as this process's home."""
    path = (tmp_path / "home").resolve()
    path.mkdir(mode=0o700)
    (path / ".env").write_text(f"AVA_CLUSTER_SECRET={_OLD}\n")
    (path / ".env").chmod(0o600)
    monkeypatch.setattr("shared.paths.ava_home", lambda: path)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _OLD)
    return path


def _secret(home: Path) -> str:
    return dotenv_values(home / ".env")["AVA_CLUSTER_SECRET"] or ""


class _Journal:
    """The save callback: records every state and what `.env` held at that moment."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.saved: list[rotate.Rotation] = []
        self.env_at_save: list[str] = []

    def __call__(self, rotation: rotate.Rotation) -> None:
        self.saved.append(rotation)
        self.env_at_save.append(_secret(self.home))


def test_rotation_pins_the_passphrase_before_the_secret_changes(home: Path) -> None:
    journal = _Journal(home)
    done = rotate.advance(home, None, journal)

    assert [r.state for r in journal.saved] == ["pinning", "pinned", "done"]
    # The secret changes only after `pinned` was recorded.
    assert journal.env_at_save == [_OLD, _OLD, _secret(home)]
    new = _secret(home)
    assert new != _OLD and done.state == "done"
    assert passphrase.pinned(home) == passphrase.derive(_OLD)
    assert passphrase.pin_path(home).stat().st_mode & 0o777 == 0o600
    assert not rotate.pending_path(home).exists()
    # The journal holds fingerprints only: no secret, no passphrase.
    recorded = json.dumps([asdict(r) for r in journal.saved])
    for value in (_OLD, new, passphrase.derive(_OLD)):
        assert value not in recorded


def _fake_pg_dump(real: Callable[..., Any]) -> Callable[..., Any]:
    """Run the real encryption pipeline over a fake dump (no database needed)."""

    def run(argv: list[str], **kwargs: Any) -> Any:
        if Path(argv[0]).name == "pg_dump":
            Path(argv[argv.index("--file") + 1]).write_bytes(b"PGDMP fake dump")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return real(argv, **kwargs)

    return run


@pytest.fixture
def backups(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[], Path]:
    """Take one logical backup through `run_backup` (real openssl encryption)."""
    directory = tmp_path / "backups-db"
    monkeypatch.setattr(backup, "backup_dir", lambda: directory)
    monkeypatch.setattr(backup, "_run_with_progress", _fake_pg_dump(backup._run_with_progress))
    stamps = iter(range(1, 60))

    def take() -> Path:
        now = datetime(2026, 9, 27, 3, next(stamps), tzinfo=UTC)
        staging = directory / f"stage-{now.minute}"
        return backup.run_backup(now, db_url="dbname=ava", publish=False, staging=staging)

    return take


def _restored(artifact: Path) -> bytes:
    """The restore path's decryption (`decrypt_artifact`, shared by the snapshot
    verification and the restore drill); a fresh output file per call."""
    out = artifact.parent / f"restored-{next(_RESTORES)}.dump"
    backup.decrypt_artifact(artifact, out)
    return out.read_bytes()


def test_backups_from_before_and_after_the_rotation_restore_with_one_key(
    home: Path, backups: Callable[[], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = backups()
    assert _restored(before) == b"PGDMP fake dump"
    rotate.advance(home, None, lambda _rotation: None)
    # The restarted gateway runs with the new secret.
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _secret(home))
    after = backups()
    assert _restored(before) == b"PGDMP fake dump"  # the restore path, old artifact
    assert _restored(after) == b"PGDMP fake dump"  # the backup path, same pinned key
    # A second rotation keeps the first pin: the key never changes again.
    rotate.advance(home, None, lambda _rotation: None)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", _secret(home))
    assert _restored(before) == _restored(backups()) == b"PGDMP fake dump"


def test_without_a_pin_a_rotated_secret_cannot_restore_older_backups(
    home: Path, backups: Callable[[], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control that makes the pin load-bearing: derivation alone fails."""
    before = backups()
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "rotated-" + "n" * 32)
    with pytest.raises(RuntimeError, match="backup decrypt exited"):
        _restored(before)


def _crash_after_pin(home: Path, monkeypatch: pytest.MonkeyPatch) -> rotate.Rotation:
    """Run a rotation that dies after `pinned` was recorded, before `.env` changes."""
    journal = _Journal(home)

    def crash(_home: Path, _rotation: rotate.Rotation) -> None:
        raise OSError("injected crash between pin and rotate")

    with monkeypatch.context() as scoped:
        scoped.setattr(rotate, "_write_secret", crash)
        with pytest.raises(OSError, match="injected crash"):
            rotate.advance(home, None, journal)
    assert journal.saved[-1].state == "pinned" and _secret(home) == _OLD
    return journal.saved[-1]


def test_a_resumed_rotation_verifies_the_pin_and_completes(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _crash_after_pin(home, monkeypatch)
    pinned = passphrase.pinned(home)
    done = rotate.advance(home, recorded, lambda _rotation: None)
    assert done.state == "done" and _secret(home) != _OLD
    assert rotate.bearer_fingerprint(_secret(home)) == recorded.new
    assert passphrase.pinned(home) == pinned == passphrase.derive(_OLD)


def test_a_resumed_rotation_never_rederives_from_the_new_secret(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After `.env` already holds the new secret, a lost pin is refused: deriving
    it now would pin the NEW secret's passphrase and orphan every old backup."""
    recorded = _crash_after_pin(home, monkeypatch)
    rotate._write_secret(home, recorded)  # the crash landed after the .env write
    passphrase.pin_path(home).unlink()
    with pytest.raises(rotate.RotationRefusedError, match="cannot be derived safely"):
        rotate.advance(home, recorded, lambda _rotation: None)
    assert passphrase.pinned(home) is None


def test_a_pin_that_contradicts_the_record_is_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded = _crash_after_pin(home, monkeypatch)
    passphrase.pin_path(home).write_text("f" * 64 + "\n")
    with pytest.raises(rotate.RotationRefusedError, match="pins another passphrase"):
        rotate.advance(home, recorded, lambda _rotation: None)
    assert _secret(home) == _OLD


def test_an_open_api_has_nothing_to_rotate(home: Path) -> None:
    (home / ".env").write_text("AVA_CLUSTER_SECRET=\n")
    with pytest.raises(rotate.RotationRefusedError, match="open API"):
        rotate.advance(home, None, lambda _rotation: None)
    assert passphrase.pinned(home) is None


def test_the_command_journals_and_resumes(home: Path) -> None:
    assert rotate.main(["--execute", "--yes"]) == 0
    first = rotate.read_journal(home)
    assert first is not None and first.state == "done"
    assert rotate.main([]) == 0  # dry run changes nothing
    assert rotate.read_journal(home) == first
