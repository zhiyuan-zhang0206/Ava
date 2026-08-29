"""Pre-update PostgreSQL snapshot guards for migration-bearing rollouts."""

from __future__ import annotations

import gzip
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

import shared.pg_tools
from cli.commands import _cluster_rollback as _rollback
from cli.commands import _update_git as _git
from services import backup


def _patch_migration_sets(
    monkeypatch: pytest.MonkeyPatch, target: set[str], current: set[str]
) -> None:
    def _target_migrations(_sha: str) -> set[str]:
        return target

    monkeypatch.setattr(_rollback, "_migration_set_at_commit", _target_migrations)
    monkeypatch.setattr(_git, "current_schema_state", lambda: current)


def _pg_restore_path(_tool: str) -> Path:
    return Path("/fake/pg_restore")


def _patch_decrypt_and_listing(
    monkeypatch: pytest.MonkeyPatch, listing: SimpleNamespace, *, legacy_gzip: bool = False
) -> None:
    """Fake decryption writing a current-format raw dump; with `legacy_gzip`,
    a gzip-compressed one that the verify path's `gunzip_if_needed` strips
    using the real gzip binary."""

    def _decrypt(_artifact: Path, custom_dump: Path) -> None:
        payload = gzip.compress(b"custom pg dump") if legacy_gzip else b"custom pg dump"
        custom_dump.write_bytes(payload)

    def _run_bounded(command: list[str], **kwargs: object) -> object:
        _ = command, kwargs
        return listing

    monkeypatch.setattr(backup, "decrypt_artifact", _decrypt)
    monkeypatch.setattr(shared.pg_tools, "pg_tool", _pg_restore_path)
    monkeypatch.setattr(_git, "run_bounded", _run_bounded)


def test_code_only_update_skips_pre_update_data_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equal migration sets cannot damage data, so no dump is made or verified."""
    migrations = {"00000000T000000_baseline"}
    _patch_migration_sets(monkeypatch, migrations, migrations)

    def _unexpected_backup(**_kw: object) -> NoReturn:
        pytest.fail("code-only update must not create a data snapshot")

    def _unexpected_pg_restore(*_args: object, **_kw: object) -> NoReturn:
        pytest.fail("code-only update must not invoke pg_restore")

    monkeypatch.setattr(backup, "run_backup", _unexpected_backup)
    monkeypatch.setattr(_git, "run_bounded", _unexpected_pg_restore)

    assert _git.snapshot_pre_update_data("TARGETSHA") is None


def test_migration_update_creates_and_verifies_pre_update_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A migration delta makes a bounded managed dump and proves it has a TOC.

    The verified path is printed to the rollout output so the snapshot is
    visible in the log — without the line, an operator mistakes it for an
    unscheduled backup (2026-08-26 incident).
    """
    _patch_migration_sets(monkeypatch, {"baseline", "20260825T010101_expand"}, {"baseline"})
    dump = tmp_path / "pre-update.dump"
    dump.write_bytes(b"custom-pg-dump")
    backup_calls: list[float] = []
    verified: list[Path] = []

    def _run_backup(*, timeout_s: float, pre_update: bool) -> Path:
        backup_calls.append(timeout_s)
        assert pre_update is True
        return dump

    monkeypatch.setattr(backup, "run_backup", _run_backup)
    monkeypatch.setattr(_git, "_verify_snapshot_artifact", verified.append)

    assert _git.snapshot_pre_update_data("TARGETSHA") == dump
    assert backup_calls == [_git._PRE_UPDATE_DUMP_TIMEOUT_S]
    assert verified == [dump]
    out = capsys.readouterr().out
    assert f"→ pre-update data snapshot: {dump} (verified)" in out


def test_pre_update_data_snapshot_wraps_backup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration-bearing rollout fails before the stop when pg_dump cannot run."""
    _patch_migration_sets(monkeypatch, {"baseline", "20260825T010101_expand"}, {"baseline"})

    def _run_backup(*, timeout_s: float, pre_update: bool) -> Path:
        _ = timeout_s
        assert pre_update is True
        raise RuntimeError("pg_dump failed")

    monkeypatch.setattr(backup, "run_backup", _run_backup)

    with pytest.raises(RuntimeError, match="could not create pre-update data snapshot"):
        _git.snapshot_pre_update_data("TARGETSHA")


def test_pre_update_data_snapshot_never_exposes_backup_db_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pg_dump failure cannot leak its credential-bearing argv into rollout logs."""
    _patch_migration_sets(monkeypatch, {"baseline", "20260825T010101_expand"}, {"baseline"})
    db_url = "postgresql://ava:secret-token@db.example/ava"

    def _run_backup(*, timeout_s: float, pre_update: bool) -> Path:
        _ = pre_update
        raise subprocess.TimeoutExpired(["pg_dump", "--dbname", db_url], timeout_s)

    monkeypatch.setattr(backup, "run_backup", _run_backup)

    with pytest.raises(RuntimeError) as caught:
        _git.snapshot_pre_update_data("TARGETSHA")

    assert db_url not in str(caught.value)


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, "archive listing"), (0, "")],
    ids=("pg-restore-fails", "empty-toc"),
)
def test_pre_update_data_snapshot_rejects_unrestorable_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    """A decrypted dump must both list successfully and contain a non-empty TOC."""
    artifact = tmp_path / "pre-update.dump.gz.enc"
    artifact.write_bytes(b"encrypted artifact")
    _patch_decrypt_and_listing(
        monkeypatch, SimpleNamespace(returncode=returncode, stdout=stdout, stderr="bad dump")
    )

    with pytest.raises(RuntimeError, match=str(artifact)):
        _git._verify_snapshot_artifact(artifact)


def test_pre_update_data_snapshot_rejects_header_only_toc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pg_restore's comments alone are not a restorable archive table of contents."""
    artifact = tmp_path / "pre-update.dump.gz.enc"
    artifact.write_bytes(b"encrypted artifact")
    header = "; Archive created at 2026-08-25\n;\n"
    _patch_decrypt_and_listing(monkeypatch, SimpleNamespace(returncode=0, stdout=header, stderr=""))

    with pytest.raises(RuntimeError, match="empty table of contents"):
        _git._verify_snapshot_artifact(artifact)


def test_pre_update_data_snapshot_verifies_legacy_gzip_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-double-gzip-removal artifact (decrypted bytes are gzip) still
    verifies: `gunzip_if_needed` strips the legacy layer before the TOC check."""
    artifact = tmp_path / "pre-update.dump.gz.enc"
    artifact.write_bytes(b"encrypted artifact")
    _patch_decrypt_and_listing(
        monkeypatch,
        SimpleNamespace(returncode=0, stdout="1; TABLE data", stderr=""),
        legacy_gzip=True,
    )

    _git._verify_snapshot_artifact(artifact)  # must not raise


def test_pre_update_data_snapshot_rejects_empty_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty encrypted artifact is never a restore point."""
    artifact = tmp_path / "pre-update.dump.gz.enc"
    artifact.write_bytes(b"")

    def _unexpected_pg_restore(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("empty dump must not be sent to pg_restore")

    monkeypatch.setattr(_git, "run_bounded", _unexpected_pg_restore)

    with pytest.raises(RuntimeError, match=str(artifact)):
        _git._verify_snapshot_artifact(artifact)


def test_pre_update_data_snapshot_holds_backup_lock_through_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduled writer cannot sweep or replace a dump before its TOC check finishes."""
    _patch_migration_sets(monkeypatch, {"baseline", "20260825T010101_expand"}, {"baseline"})
    dump = tmp_path / "pre-update.dump"
    dump.write_bytes(b"custom-pg-dump")
    events: list[str] = []

    @contextmanager
    def _backup_lock(*, timeout_s: float) -> Generator[None]:
        assert timeout_s == _git._PRE_UPDATE_DUMP_TIMEOUT_S
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    def _run_backup(*, timeout_s: float, pre_update: bool) -> Path:
        assert timeout_s == _git._PRE_UPDATE_DUMP_TIMEOUT_S
        assert pre_update is True
        events.append("dump")
        return dump

    def _verify(artifact: Path) -> None:
        assert artifact == dump
        events.append("verify")

    monkeypatch.setattr(backup, "backup_lock", _backup_lock, raising=False)
    monkeypatch.setattr(backup, "run_backup", _run_backup)
    monkeypatch.setattr(_git, "_verify_snapshot_artifact", _verify)

    assert _git.snapshot_pre_update_data("TARGETSHA") == dump
    assert events == ["lock-enter", "dump", "verify", "lock-exit"]


def test_pre_update_data_snapshot_propagates_pre_cutover_target_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Target validation owns pre-cutover rejection; this helper does not mask it."""

    def _pre_cutover(_sha: str) -> set[str]:
        raise ValueError("pre-cutover target")

    monkeypatch.setattr(_rollback, "_migration_set_at_commit", _pre_cutover)

    with pytest.raises(ValueError, match="pre-cutover target"):
        _git.snapshot_pre_update_data("TARGETSHA")


def test_pre_activation_snapshot_uses_activation_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pre-activation logical floor carries the PITR-activation kind — the
    marker prune exempts from rotation. QA #944 block: the kind markers were
    swapped between the rollout and activation helpers; the strict fake
    signature trips (TypeError) if `pitr_activation` is ever replaced by the
    pre-update marker again."""
    dump = tmp_path / "pitr-activation.dump"
    dump.write_bytes(b"custom-pg-dump")
    calls: list[float] = []
    verified: list[Path] = []

    def _run_backup(*, timeout_s: float, pitr_activation: bool) -> Path:
        calls.append(timeout_s)
        assert pitr_activation is True
        return dump

    monkeypatch.setattr(backup, "run_backup", _run_backup)
    monkeypatch.setattr(_git, "_verify_snapshot_artifact", verified.append)

    assert _git.snapshot_pre_activation_data() == dump
    assert calls == [_git._PRE_UPDATE_DUMP_TIMEOUT_S]
    assert verified == [dump]
    out = capsys.readouterr().out
    assert f"→ pre-activation data snapshot: {dump} (verified)" in out
