"""The logical-backup passphrase is pinned, never derived; a bearer rotation keeps it.

A gateway birth mints and pins the passphrase, so rotating the cluster secret
never changes the key. A home born earlier encrypted under `sha256(secret)`;
with exactly that pinned, its earlier artifacts keep decrypting. An empty
secret's derivation is a public constant, so such a home carries a minted
passphrase and its earlier artifacts decrypt only through the explicit legacy
restore option. Every rotation step is journaled before its effect, with
fingerprints only, and a resumed rotation never re-derives the passphrase from
the new secret.
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

from base.config import settings
from base.db import Database
from scripts.data_plane_ops import rotate_cluster_secret as rotate
from services.backup import dump as backup
from services.backup.artifact import passphrase

_OLD = "old-bearer-" + "o" * 32
_RESTORES = itertools.count()


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret: str) -> Path:
    path = (tmp_path / "home").resolve()
    path.mkdir(mode=0o700)
    (path / ".env").write_text(f"AVA_CLUSTER_SECRET={secret}\n")
    (path / ".env").chmod(0o600)
    monkeypatch.setattr("base.paths.ava_home", lambda: path)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", secret)
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A gateway home born before births minted a passphrase (none pinned),
    whose `.env` holds `_OLD`, as this process's home."""
    return _home(tmp_path, monkeypatch, _OLD)


@pytest.fixture
def born(home: Path) -> Path:
    """The same home as a birth leaves it: a minted passphrase pinned."""
    passphrase.ensure_minted(home)
    return home


@pytest.fixture
def open_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty-secret single box born before births minted a passphrase."""
    return _home(tmp_path, monkeypatch, "")


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


def test_a_born_homes_rotation_keeps_its_minted_passphrase(born: Path) -> None:
    minted = passphrase.pinned(born)
    done = rotate.advance(born, None, lambda _rotation: None)
    assert done.state == "done" and _secret(born) != _OLD
    assert passphrase.pinned(born) == minted
    assert minted not in (passphrase.derive(_OLD), passphrase.derive(_secret(born)))


def _fake_pg_dump(real: Callable[..., Any]) -> Callable[..., Any]:
    """Run the real encryption pipeline over a fake dump (no database needed)."""

    def run(argv: list[str], **kwargs: Any) -> Any:
        if Path(argv[0]).name == "pg_dump":
            Path(argv[argv.index("--file") + 1]).write_bytes(b"PGDMP fake dump")
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return real(argv, **kwargs)

    return run


@pytest.fixture
def backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database: Database
) -> Callable[[], Path]:
    """Take one logical backup of this process's home through `run_backup`
    (real openssl encryption)."""
    directory = tmp_path / "backups-db"
    monkeypatch.setattr(backup, "backup_dir", lambda: directory)
    monkeypatch.setattr(backup, "_run_with_progress", _fake_pg_dump(backup._run_with_progress))
    stamps = iter(range(1, 60))

    def take() -> Path:
        now = datetime(2026, 9, 27, 3, next(stamps), tzinfo=UTC)
        staging = directory / f"stage-{now.minute}"
        return backup.run_backup(
            now, db_url="dbname=ava", publish=False, staging=staging, db=database
        )

    return take


def _restored(artifact: Path, *, legacy_empty_secret: bool = False) -> bytes:
    """The restore path's decryption (`decrypt_artifact`, shared by the snapshot
    verification and the restore drill); a fresh output file per call."""
    out = artifact.parent / f"restored-{next(_RESTORES)}.dump"
    backup.decrypt_artifact(artifact, out, legacy_empty_secret=legacy_empty_secret)
    return out.read_bytes()


def _decrypts_with(artifact: Path, key: str) -> bool:
    """Whether plain openssl opens `artifact` under `key` (an outsider's attempt)."""
    key_file = artifact.parent / f"outsider-{next(_RESTORES)}.key"
    key_file.write_text(key)
    out = artifact.parent / f"outsider-{next(_RESTORES)}.dump"
    argv = ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-salt", "-kfile", str(key_file)]
    proc = subprocess.run(  # noqa: S603 — fixed openssl argv over test-owned files
        [*argv, "-in", str(artifact), "-out", str(out)], capture_output=True, check=False
    )
    return proc.returncode == 0 and out.read_bytes() == b"PGDMP fake dump"


def _written_before_the_pin(
    backups: Callable[[], Path], monkeypatch: pytest.MonkeyPatch, secret: str
) -> Path:
    """An artifact as a home born earlier wrote it: under `sha256(secret)`."""
    with monkeypatch.context() as scoped:
        scoped.setattr(passphrase, "logical_backup_passphrase", lambda: passphrase.derive(secret))
        return backups()


def test_rotating_the_secret_never_changes_the_backup_key(
    born: Path, backups: Callable[[], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = backups()
    assert _restored(before) == b"PGDMP fake dump"
    for _ in range(2):
        rotate.advance(born, None, lambda _rotation: None)
        # The restarted gateway runs with the new secret.
        monkeypatch.setattr(settings.data_plane, "cluster_secret", _secret(born))
        assert _restored(before) == _restored(backups()) == b"PGDMP fake dump"


def test_a_home_without_a_pin_has_no_backup_key(home: Path, backups: Callable[[], Path]) -> None:
    """No derivation fallback: without a pin nothing is written or read."""
    with pytest.raises(passphrase.PassphrasePinError, match="no logical-backup passphrase"):
        backups()
    with pytest.raises(passphrase.PassphrasePinError, match="mints one at birth"):
        passphrase.logical_backup_passphrase()
    directory = backup.backup_dir()
    assert not list(directory.rglob("*.dump.enc")) and not list(directory.rglob(".backup-key-*"))


def test_an_empty_secret_home_encrypts_for_real(
    open_home: Path, backups: Callable[[], Path]
) -> None:
    passphrase.ensure_minted(open_home)
    artifact = backups()
    assert _restored(artifact) == b"PGDMP fake dump"
    assert not _decrypts_with(artifact, passphrase.LEGACY_EMPTY_SECRET_PASSPHRASE)
    assert _decrypts_with(artifact, passphrase.resolve(open_home))


def test_an_empty_secret_homes_earlier_artifacts_need_the_legacy_option(
    open_home: Path, backups: Callable[[], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """sha256("") is public, so such a home carries a minted passphrase; the
    artifacts written before decrypt only through the explicit legacy option,
    which is never tried on its own, and never opens a current artifact."""
    old = _written_before_the_pin(backups, monkeypatch, "")
    passphrase.ensure_minted(open_home)
    pinned = passphrase.pinned(open_home)
    assert pinned is not None and pinned != passphrase.LEGACY_EMPTY_SECRET_PASSPHRASE
    with pytest.raises(RuntimeError, match="--legacy-empty-secret-passphrase"):
        _restored(old)
    assert _restored(old, legacy_empty_secret=True) == b"PGDMP fake dump"
    current = backups()
    assert _restored(current) == b"PGDMP fake dump"
    with pytest.raises(RuntimeError, match="backup decrypt exited"):
        _restored(current, legacy_empty_secret=True)


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
