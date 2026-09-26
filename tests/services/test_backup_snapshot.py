"""Backup-owned logical recovery snapshots: locks, progress, and restore proof."""

from __future__ import annotations

import gzip
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import pytest

import shared.pg_tools
from services import backup
from services.gateway_side.backup import snapshot as _snapshot
from shared.platform import LockTimeoutError


def _snapshot_progress(line: str) -> None:
    sys.stdout.write(f"→ pre-update data snapshot: {line}\n")


def _activation_progress(line: str) -> None:
    sys.stdout.write(f"→ pre-activation data snapshot: {line}\n")


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
    monkeypatch.setattr(_snapshot, "run_bounded", _run_bounded)


def test_pre_update_data_snapshot_narrates_the_dump_on_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dump's heartbeat reaches stdout — what the rollout `tee` turns into the
    log the stall watchdog reads — so a slow snapshot reads as progress, not silence."""
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
    monkeypatch.setattr(_snapshot, "verify_snapshot", _no_verify)

    assert _snapshot.create_pre_update_snapshot(progress=_snapshot_progress) == dump

    out = capsys.readouterr().out
    assert "→ pre-update data snapshot: started (dump bounded at 20 min)" in out
    assert "→ pre-update data snapshot: pg_dump started (bounded at 20 min)" in out
    assert "→ pre-update data snapshot: pg_dump 61s, 512.4 MiB written" in out
    assert f"→ pre-update data snapshot: {dump} (verified)" in out


def test_pre_update_data_snapshot_wraps_backup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration-bearing rollout fails before the stop when pg_dump cannot run."""

    def _run_backup(
        *,
        timeout_s: float,
        pre_update: bool,
        publish: bool,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        _ = timeout_s, publish, progress
        assert pre_update is True
        raise RuntimeError("pg_dump failed")

    monkeypatch.setattr(backup, "run_backup", _run_backup)

    with pytest.raises(RuntimeError, match="could not create pre-update data snapshot"):
        _snapshot.create_pre_update_snapshot(progress=_snapshot_progress)


def test_pre_update_data_snapshot_never_exposes_backup_db_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pg_dump failure cannot leak its credential-bearing argv into rollout logs."""
    db_url = "postgresql://ava:secret-token@db.example/ava"

    def _run_backup(
        *,
        timeout_s: float,
        pre_update: bool,
        publish: bool,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        _ = pre_update, publish, progress
        raise subprocess.TimeoutExpired(["pg_dump", "--dbname", db_url], timeout_s)

    monkeypatch.setattr(backup, "run_backup", _run_backup)

    with pytest.raises(RuntimeError) as caught:
        _snapshot.create_pre_update_snapshot(progress=_snapshot_progress)

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
        _snapshot.verify_snapshot(artifact)


def test_pre_update_data_snapshot_rejects_header_only_toc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pg_restore's comments alone are not a restorable archive table of contents."""
    artifact = tmp_path / "pre-update.dump.gz.enc"
    artifact.write_bytes(b"encrypted artifact")
    header = "; Archive created at 2026-08-25\n;\n"
    _patch_decrypt_and_listing(monkeypatch, SimpleNamespace(returncode=0, stdout=header, stderr=""))

    with pytest.raises(RuntimeError, match="empty table of contents"):
        _snapshot.verify_snapshot(artifact)


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

    _snapshot.verify_snapshot(artifact)  # must not raise


def test_pre_update_data_snapshot_rejects_empty_dump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty encrypted artifact is never a restore point."""
    artifact = tmp_path / "pre-update.dump.gz.enc"
    artifact.write_bytes(b"")

    def _unexpected_pg_restore(*_args: object, **_kwargs: object) -> NoReturn:
        pytest.fail("empty dump must not be sent to pg_restore")

    monkeypatch.setattr(_snapshot, "run_bounded", _unexpected_pg_restore)

    with pytest.raises(RuntimeError, match=str(artifact)):
        _snapshot.verify_snapshot(artifact)


def test_pre_update_data_snapshot_holds_backup_lock_through_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduled writer cannot sweep or replace a dump before its TOC check finishes."""
    dump = tmp_path / "pre-update.dump"
    dump.write_bytes(b"custom-pg-dump")
    events: list[str] = []

    @contextmanager
    def _backup_lock(*, timeout_s: float) -> Generator[None]:
        assert timeout_s == min(_snapshot.LOCK_HEARTBEAT_S, _snapshot.DUMP_TIMEOUT_S)
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
        assert timeout_s == _snapshot.DUMP_TIMEOUT_S
        assert pre_update is True
        assert publish is False
        events.append("dump")
        return dump

    def _verify(artifact: Path) -> None:
        assert artifact == dump
        events.append("verify")

    monkeypatch.setattr(backup, "backup_lock", _backup_lock, raising=False)
    monkeypatch.setattr(backup, "run_backup", _run_backup)
    monkeypatch.setattr(_snapshot, "verify_snapshot", _verify)

    assert _snapshot.create_pre_update_snapshot(progress=_snapshot_progress) == dump
    assert events == ["lock-enter", "dump", "verify", "lock-exit"]


def test_pre_update_data_snapshot_lock_wait_narrates_heartbeats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A contended backup lock narrates one line per heartbeat chunk — the wait
    is not log silence — and the snapshot proceeds once the lock frees."""
    dump = tmp_path / "pre-update.dump"
    dump.write_bytes(b"custom-pg-dump")
    attempts: list[float] = []
    held_chunks = 2

    @contextmanager
    def _backup_lock(*, timeout_s: float) -> Generator[None]:
        attempts.append(timeout_s)
        if len(attempts) <= held_chunks:
            raise LockTimeoutError("another backup holds it")
        yield

    def _run_backup(
        *,
        timeout_s: float,
        pre_update: bool,
        publish: bool,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        _ = timeout_s, pre_update, publish, progress
        return dump

    def _no_verify(_artifact: Path) -> None:
        pass

    monkeypatch.setattr(backup, "backup_lock", _backup_lock)
    monkeypatch.setattr(backup, "run_backup", _run_backup)
    monkeypatch.setattr(_snapshot, "verify_snapshot", _no_verify)

    assert _snapshot.create_pre_update_snapshot(progress=_snapshot_progress) == dump

    assert attempts == [_snapshot.LOCK_HEARTBEAT_S] * (held_chunks + 1)
    out = capsys.readouterr().out
    assert out.count("→ pre-update data snapshot: waiting for the backup lock") == held_chunks
    assert "another backup is writing)" in out


def test_pre_update_data_snapshot_lock_wait_gives_up_at_the_total_budget(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lock held for the whole budget still expires — the raise names the
    total `DUMP_TIMEOUT_S`, not the final `LOCK_HEARTBEAT_S`
    chunk — and the dump never starts."""
    clock = [0.0]
    attempts: list[float] = []

    @contextmanager
    def _held_lock(*, timeout_s: float) -> Generator[None]:
        attempts.append(timeout_s)
        clock[0] += timeout_s  # each take waits out its chunk, then expires
        raise LockTimeoutError("another backup holds it")

    def _now() -> float:
        return clock[0]

    def _unexpected_backup(**_kwargs: object) -> NoReturn:
        pytest.fail("the dump must not start while the lock is held")

    monkeypatch.setattr(_snapshot, "time", SimpleNamespace(monotonic=_now))
    monkeypatch.setattr(backup, "backup_lock", _held_lock)
    monkeypatch.setattr(backup, "run_backup", _unexpected_backup)

    with pytest.raises(LockTimeoutError, match=f"within {_snapshot.DUMP_TIMEOUT_S:.0f}s"):
        _snapshot.create_pre_update_snapshot(progress=_snapshot_progress)

    expected_chunks = int(_snapshot.DUMP_TIMEOUT_S / _snapshot.LOCK_HEARTBEAT_S)
    assert attempts == [_snapshot.LOCK_HEARTBEAT_S] * expected_chunks
    out = capsys.readouterr().out
    assert out.count("→ pre-update data snapshot: waiting for the backup lock") == expected_chunks


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
    sinks: list[Callable[[str], None] | None] = []

    def _run_backup(
        *,
        timeout_s: float,
        pitr_activation: str,
        db_url: str,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        calls.append(timeout_s)
        sinks.append(progress)
        assert pitr_activation == "11111111-1111-1111-1111-111111111111"
        assert db_url == "dbname=ava"
        return dump

    monkeypatch.setattr(backup, "run_backup", _run_backup)
    monkeypatch.setattr(_snapshot, "verify_snapshot", verified.append)

    assert (
        _snapshot.create_pre_activation_snapshot(
            operation_id="11111111-1111-1111-1111-111111111111",
            db_url="dbname=ava",
            progress=_activation_progress,
        )
        == dump
    )
    assert calls == [_snapshot.DUMP_TIMEOUT_S]
    assert callable(sinks[0]), "the activation dump must narrate (task #3442)"
    assert verified == [dump]
    out = capsys.readouterr().out
    assert f"→ pre-activation data snapshot: {dump} (verified)" in out


def test_pre_activation_snapshot_narrates_lock_wait_and_dump_through_the_activation_sink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pre-activation snapshot narrates through the activation prefix — the
    lock wait and the dump stages (#2518 residual: its log was silent until the
    final verified line)."""
    dump = tmp_path / "pitr-activation.dump"
    dump.write_bytes(b"custom-pg-dump")
    attempts: list[float] = []

    @contextmanager
    def _backup_lock(*, timeout_s: float) -> Generator[None]:
        attempts.append(timeout_s)
        if len(attempts) == 1:
            raise LockTimeoutError("another backup holds it")
        yield

    def _run_backup(
        *,
        timeout_s: float,
        pitr_activation: str,
        db_url: str,
        progress: Callable[[str], None] | None = None,
    ) -> Path:
        _ = timeout_s, pitr_activation, db_url
        assert progress is not None
        progress("pg_dump 61s, 512.4 MiB written")
        return dump

    def _no_verify(_artifact: Path) -> None:
        pass

    monkeypatch.setattr(backup, "backup_lock", _backup_lock)
    monkeypatch.setattr(backup, "run_backup", _run_backup)
    monkeypatch.setattr(_snapshot, "verify_snapshot", _no_verify)

    assert (
        _snapshot.create_pre_activation_snapshot(
            operation_id="11111111-1111-1111-1111-111111111111",
            db_url="dbname=ava",
            progress=_activation_progress,
        )
        == dump
    )
    assert attempts == [_snapshot.LOCK_HEARTBEAT_S] * 2
    out = capsys.readouterr().out
    bounded = f"{_snapshot.DUMP_TIMEOUT_S / 60:.0f} min"
    assert f"→ pre-activation data snapshot: started (dump bounded at {bounded})" in out
    assert "→ pre-activation data snapshot: waiting for the backup lock" in out
    assert "→ pre-activation data snapshot: pg_dump 61s, 512.4 MiB written" in out
    assert f"→ pre-activation data snapshot: {dump} (verified)" in out


def test_interrupted_snapshot_dump_leaves_no_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot interrupted mid-dump (stop, Ctrl-C, timeout) kills and reaps
    its writer before the plaintext partial is removed."""
    monkeypatch.setattr(backup, "backup_dir", lambda: tmp_path)

    def composition(_db_url: str | None = None) -> str:
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
        backup.run_backup(db_url="dbname=ava", publish=False, progress=interrupt)
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed in-process snapshot's plaintext partial never outlives the next
    run: the scheduled (staged) worker run sweeps the backup directory too, and
    the same sweep removes key files and an abandoned cross-filesystem copy."""
    from datetime import UTC, datetime

    published = tmp_path / "db"
    published.mkdir(mode=0o700)
    monkeypatch.setattr(backup, "backup_dir", lambda: published)

    def composition(_db_url: str | None = None) -> str:
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
    activation = "00000000-0000-0000-0000-000000000000"
    stale = [
        published / f"ava-20260920T030000Z.pitr-activation-{activation}.dump.partial",
        published / ".backup-key-stale",
        published / ".ava-20260925T030000Z.dump.enc.k3y9.copy",
    ]
    for path in stale:
        path.write_bytes(b"PLAINTEXT")
    staging = tmp_path / "controls" / "artifact"
    artifact = backup.run_backup(
        datetime(2026, 9, 26, 3, tzinfo=UTC), db_url="dbname=ava", publish=False, staging=staging
    )
    assert artifact.parent == staging
    assert [path.name for path in stale if path.exists()] == []
    assert list(published.iterdir()) == []
