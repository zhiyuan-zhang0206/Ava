"""Pre-update PITR must prove continuity and remote identity, not just a flag."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cli.commands import _cluster_rollback, _update_dryrun, _update_git, _update_pitr
from cli.commands._update_recover import _print_pre_update_data_snapshot_restore
from services import backup
from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
from services.pitr.restore_manifest import (
    ProtectedManifest,
    RestoreObject,
    RestoreProof,
    candidate_sha256,
    required_archive_names,
)
from services.pitr.retention_inventory import InventorySnapshot
from services.pitr.retention_manifest import RetentionObject
from services.pitr.uploader import AckManifest


def _ack(name: str) -> AckManifest:
    return AckManifest(
        name,
        "sha",
        16 * 1024 * 1024,
        f"pitr/wal/{name}",
        "8",
        20,
        "wal-crc",
        "crc32c",
        "wal-crc",
        "AVAPITR1",
        "key",
        "2026-09-14T00:00:00+00:00",
    )


SHA = "a" * 40
IDENTITY = _update_pitr.DatabaseIdentity("42", "ava", 17, 1, 16 * 1024 * 1024)


def _protected(root: Path, chain: str = "20260914T000000Z") -> ProtectedManifest:
    candidate = CandidateManifest(
        1,
        chain,
        False,
        17,
        "ava",
        "42",
        IDENTITY.wal_segment_size,
        1,
        "0/1000000",
        "0/2000000",
        (WalRange(1, "0/1000000", "0/2000000"),),
        BaseObject("pitr/base", "7", 100, "crc", "crc32c", "crc", "sha", 90, "key", "AVAPITRB1"),
        "native",
        "backup_manifest",
        "pitr/base",
        "7",
        "migrations",
    )
    base = RestoreObject("base", "pitr/base", "7", 100, "crc32c", "crc", (("ava-key-id", "key"),))
    (root / "ack").mkdir(exist_ok=True)
    name = "000000010000000000000001"
    (root / "ack" / f"{name}.ack.json").write_text(json.dumps(asdict(_ack(name))))
    wal = _update_pitr._wait_wal(root, (name,))[0]
    proof = RestoreProof(
        "drill",
        "2026-09-14T00:00:00+00:00",
        "2026-09-14T00:01:00+00:00",
        candidate.end_lsn,
        candidate.end_lsn,
        1,
        "live",
        "verify",
        1,
        1,
        1,
        100,
        "fingerprint",
    )
    protected = ProtectedManifest(
        1,
        True,
        chain,
        candidate_sha256(candidate),
        candidate,
        base,
        (wal,),
        candidate.end_lsn,
        candidate.wal_segment_size,
        proof,
    )
    for directory in ("protected-manifests", "base-manifests"):
        (root / directory).mkdir(exist_ok=True)
    (root / "protected-manifests" / f"{chain}.json").write_text(protected.to_json())
    (root / "base-manifests" / f"{chain}.candidate.json").write_text(candidate.to_json())
    return protected


def test_select_latest_drilled_chain(tmp_path: Path) -> None:
    _protected(tmp_path, "20260907T000000Z")
    expected = _protected(tmp_path)
    assert _update_pitr.select_chain(tmp_path, IDENTITY) == expected


@pytest.mark.parametrize("publication_fails", [False, True])
def test_fresh_point_is_complete_and_published_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_fails: bool,
) -> None:
    root = tmp_path / "physical-backup"
    root.mkdir()
    (root / "activation").mkdir()
    proof = _protected(root)
    target = "0/3000001"
    names = required_archive_names((WalRange(1, "0/1000000", target),), IDENTITY.wal_segment_size)
    for name in names:
        (root / "ack" / f"{name}.ack.json").write_text(json.dumps(asdict(_ack(name))))
    wal = _update_pitr._wait_wal(root, names)
    remote = tuple(
        RetentionObject(
            item.object_name,
            item.pin_token,
            item.size,
            None if item == proof.base else item.archive_name,
            "base" if item == proof.base else "wal",
            item.checksum_algo,
            item.checksum_value,
            item.metadata,
        )
        for item in (proof.base, *wal)
    )
    group = MagicMock()
    group.retention_inventory_reader.return_value.snapshot.return_value = InventorySnapshot(
        remote, ()
    )
    publish = group.protected_manifest_publisher.return_value.put_manifest_if_absent
    if publication_fails:
        publish.side_effect = RuntimeError("object store unavailable")
    monkeypatch.setattr(_update_pitr, "get_store_group", lambda: group)
    monkeypatch.setattr(_update_pitr, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(_update_pitr, "pitr_admin_url", lambda: "postgresql:///postgres")
    monkeypatch.setattr(
        _update_pitr,
        "settings",
        SimpleNamespace(
            physical_backup=SimpleNamespace(
                pitr_enabled=True,
                pitr_restore_proof_enabled=True,
                pitr_backup_key_id="key",
                pitr_retained_weekly_chains=2,
                pitr_gcs_prefix="pitr",
            ),
            data_plane=SimpleNamespace(is_remote=False, db_url="postgresql:///ava"),
        ),
    )
    connection = MagicMock()
    connection.execute.return_value.fetchone.side_effect = [
        ("42", "ava", 17, 1, IDENTITY.wal_segment_size),
        (target,),
        ("42", "ava", 17, 1, IDENTITY.wal_segment_size),
    ]
    connect = MagicMock()
    connect.return_value.__enter__.return_value = connection
    monkeypatch.setattr(_update_pitr.psycopg, "connect", connect)
    if publication_fails:
        with pytest.raises(RuntimeError, match="object store unavailable"):
            _update_pitr.create_recovery_point(SHA)
        assert not list(root.glob("update-recovery/*.json"))
        return
    path = _update_pitr.create_recovery_point(SHA)
    receipt = json.loads(path.read_text())
    assert receipt["target_lsn"] == target
    assert receipt["target_sha"] == SHA
    assert len(receipt["wal"]) == 3
    assert receipt["protected_base"] == json.loads(proof.to_json())
    assert publish.call_args.kwargs["payload"] == path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert "pg_create_restore_point" in connection.execute.call_args_list[1].args[0]
    assert "pg_switch_wal" in connection.execute.call_args_list[2].args[0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("system_identifier", "43"),
        ("database_name", "foreign"),
        ("postgres_major", 18),
        ("timeline", 2),
        ("wal_segment_size", 32 * 1024 * 1024),
    ],
)
def test_wrong_database_identity_refuses(tmp_path: Path, field: str, value: object) -> None:
    _protected(tmp_path)
    with pytest.raises(RuntimeError, match="another database or timeline"):
        _update_pitr.select_chain(tmp_path, replace(IDENTITY, **{field: value}))


def test_corrupt_newest_chain_never_falls_back(tmp_path: Path) -> None:
    _protected(tmp_path, "20260907T000000Z")
    _protected(tmp_path)
    (tmp_path / "protected-manifests/20260914T000000Z.json").write_text("{}")
    with pytest.raises(ValueError):
        _update_pitr.select_chain(tmp_path, IDENTITY)


@pytest.mark.parametrize(
    "changed",
    [
        {"pin_token": "replaced"},
        {"size": 101},
        {"checksum_value": "corrupt"},
        {"metadata": (("ava-key-id", "another-key"),)},
    ],
)
def test_remote_generation_and_bytes_must_match(tmp_path: Path, changed: dict[str, object]) -> None:
    expected = _protected(tmp_path).base
    actual = RetentionObject(
        expected.object_name,
        expected.pin_token,
        expected.size,
        None,
        "base",
        expected.checksum_algo,
        expected.checksum_value,
        expected.metadata,
    )
    _update_pitr.verify_inventory((expected,), InventorySnapshot((actual,), ()))
    with pytest.raises(RuntimeError, match="missing or differs"):
        _update_pitr.verify_inventory(
            (expected,), InventorySnapshot((replace(actual, **changed),), ())
        )


def test_missing_remote_object_refuses(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="missing or differs"):
        _update_pitr.verify_inventory((_protected(tmp_path).base,), InventorySnapshot((), ()))


def test_hole_between_base_and_fresh_target_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = required_archive_names(
        (WalRange(1, "0/1000000", "0/3000001"),), IDENTITY.wal_segment_size
    )
    assert len(names) == 3
    (tmp_path / "ack").mkdir()
    for name in (names[0], names[-1]):
        (tmp_path / "ack" / f"{name}.ack.json").write_text("{}")
    monkeypatch.setattr(_update_pitr, "WAL_WAIT_SECONDS", 0)
    with pytest.raises(RuntimeError, match="1 unacknowledged"):
        _update_pitr._wait_wal(tmp_path, names)


def _enable_update_pitr(monkeypatch: pytest.MonkeyPatch) -> None:
    def migrations(_sha: str) -> set[str]:
        return {"new"}

    monkeypatch.setattr(_cluster_rollback, "_migration_set_at_commit", migrations)
    monkeypatch.setattr(_update_git, "current_schema_state", lambda: {"old"})
    monkeypatch.setattr(
        _update_git,
        "settings",
        SimpleNamespace(
            physical_backup=SimpleNamespace(pitr_enabled=True),
        ),
    )

    def no_dump(**_kwargs: object) -> None:
        pytest.fail("must not dump the database")

    monkeypatch.setattr(backup, "run_backup", no_dump)


def test_migration_with_pitr_uses_bounded_incremental_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_update_pitr(monkeypatch)
    path = tmp_path / f"receipt{_update_pitr.RECOVERY_SUFFIX}"
    path.write_text("receipt")

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[-2:] == ["cli.commands._update_pitr", SHA]
        assert kwargs["timeout"] == 600  # The rollout watchdog's no-progress deadline.
        return subprocess.CompletedProcess(command, 0, stdout=str(path), stderr="")

    monkeypatch.setattr(_update_git, "run_bounded", run)
    assert _update_git.snapshot_pre_update_data(SHA) == path


@pytest.mark.parametrize("timeout", [False, True])
def test_broken_pitr_never_falls_back_to_dump(
    monkeypatch: pytest.MonkeyPatch, timeout: bool
) -> None:
    _enable_update_pitr(monkeypatch)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if timeout:
            raise subprocess.TimeoutExpired(command, 600)
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="private-sdk-detail")

    monkeypatch.setattr(_update_git, "run_bounded", run)
    with pytest.raises(RuntimeError, match="PITR pre-update") as caught:
        _update_git.snapshot_pre_update_data(SHA)
    assert "private-sdk-detail" not in str(caught.value)


def test_physical_receipt_is_not_uploaded_or_restored_as_logical_dump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / f"receipt{_update_pitr.RECOVERY_SUFFIX}"

    def no_upload(*_args: object, **_kwargs: object) -> None:
        pytest.fail("already offsite")

    monkeypatch.setattr(_update_dryrun.subprocess, "Popen", no_upload)
    _update_dryrun.spawn_async_offsite_upload(tmp_path, path)
    _print_pre_update_data_snapshot_restore(path)
    assert "whole-instance recovery" in capsys.readouterr().err
