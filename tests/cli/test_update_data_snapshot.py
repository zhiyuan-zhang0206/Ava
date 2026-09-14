"""Pre-update PostgreSQL snapshot guards for migration-bearing rollouts."""

from __future__ import annotations

import gzip
import subprocess
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

import shared.pg_tools
from cli.commands import _cluster_rollback as _rollback
from cli.commands import _update_git as _git
from cli.commands import _update_local as _local
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


def test_gateway_local_update_requires_prepare_snapshot_for_pull() -> None:
    """The stop/check-out leg cannot create a snapshot after maintenance begins."""
    with pytest.raises(ValueError, match="pull_recover"):
        _local._run_gateway_local_update(
            Path("/unused"),
            target_sha="TARGETSHA",
            pull=True,
            pull_recover=None,
        )


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
    sinks: list[Callable[[str], None] | None] = []

    def _run_backup(
        *,
        timeout_s: float,
        pre_update: bool,
        publish: bool,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        backup_calls.append(timeout_s)
        sinks.append(progress)
        assert pre_update is True
        assert publish is False
        return dump

    monkeypatch.setattr(backup, "run_backup", _run_backup)
    monkeypatch.setattr(_git, "_verify_snapshot_artifact", verified.append)

    assert _git.snapshot_pre_update_data("TARGETSHA") == dump
    assert backup_calls == [_git._PRE_UPDATE_DUMP_TIMEOUT_S]
    assert callable(sinks[0]), "the dump must be narrated for the stall watchdog"
    assert verified == [dump]
    out = capsys.readouterr().out
    assert f"→ pre-update data snapshot: {dump} (verified)" in out


def test_pre_update_data_snapshot_narrates_the_dump_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dump's heartbeat reaches stdout — what the rollout `tee` turns into the
    log the stall watchdog reads — so a slow snapshot reads as progress, not silence."""
    _patch_migration_sets(monkeypatch, {"baseline", "20260825T010101_expand"}, {"baseline"})
    dump = tmp_path / "pre-update.dump"

    def _run_backup(
        *,
        timeout_s: float,
        pre_update: bool,
        publish: bool,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        _ = timeout_s, pre_update, publish
        assert progress is not None
        progress("pg_dump started (bounded at 20 min)")
        progress("pg_dump 61s, 512.4 MiB written")
        return dump

    def _no_verify(_artifact: Path) -> None:
        pass

    monkeypatch.setattr(backup, "run_backup", _run_backup)
    monkeypatch.setattr(_git, "_verify_snapshot_artifact", _no_verify)

    assert _git.snapshot_pre_update_data("TARGETSHA") == dump

    out = capsys.readouterr().out
    assert "→ pre-update data snapshot: started (pg_dump before the stop may take up to" in out
    assert "→ pre-update data snapshot: pg_dump started (bounded at 20 min)" in out
    assert "→ pre-update data snapshot: pg_dump 61s, 512.4 MiB written" in out
    assert f"→ pre-update data snapshot: {dump} (verified)" in out


def test_snapshot_heartbeat_cadence_leaves_headroom_inside_the_stall_window() -> None:
    """The dump heartbeat must beat far inside the rollout stall window.

    The 2026-09-14 kill was a healthy 20-minute dump sitting silent past the
    900 s no-progress clock; the cadence is the fix's premise, so it is pinned
    here rather than left to drift apart from either constant.
    """
    from shared.deploy_timing import NO_PROGRESS_TIMEOUT_S

    assert backup._PROGRESS_INTERVAL_S * 3 <= NO_PROGRESS_TIMEOUT_S


def test_pre_update_data_snapshot_wraps_backup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration-bearing rollout fails before the stop when pg_dump cannot run."""
    _patch_migration_sets(monkeypatch, {"baseline", "20260825T010101_expand"}, {"baseline"})

    def _run_backup(
        *,
        timeout_s: float,
        pre_update: bool,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        _ = timeout_s, progress
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

    def _run_backup(
        *,
        timeout_s: float,
        pre_update: bool,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        _ = pre_update, progress
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

    def _run_backup(
        *,
        timeout_s: float,
        pre_update: bool,
        publish: bool,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        assert callable(progress)
        assert timeout_s == _git._PRE_UPDATE_DUMP_TIMEOUT_S
        assert pre_update is True
        assert publish is False
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

    def _run_backup(*, timeout_s: float, pitr_activation: str, db_url: str) -> Path:
        calls.append(timeout_s)
        assert pitr_activation == "11111111-1111-1111-1111-111111111111"
        assert db_url == "dbname=ava"
        return dump

    monkeypatch.setattr(backup, "run_backup", _run_backup)
    monkeypatch.setattr(_git, "_verify_snapshot_artifact", verified.append)

    assert (
        _git.snapshot_pre_activation_data(
            operation_id="11111111-1111-1111-1111-111111111111",
            db_url="dbname=ava",
        )
        == dump
    )
    assert calls == [_git._PRE_UPDATE_DUMP_TIMEOUT_S]
    assert verified == [dump]
    out = capsys.readouterr().out
    assert f"→ pre-activation data snapshot: {dump} (verified)" in out
