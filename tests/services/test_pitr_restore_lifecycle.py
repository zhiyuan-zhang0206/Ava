from __future__ import annotations

import json
from pathlib import Path

import psutil
from pytest import MonkeyPatch

from services.pitr import restore_manifest, restore_proof
from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
from services.pitr.object_store import RemoteObjectAck
from services.pitr.restore_manifest import RestoreObject
from services.pitr.restore_postgres import _write_sandbox_config
from services.pitr.restore_proof import (
    DrillResult,
    LivePostgresIdentity,
    RestoreSpaceBudget,
    prove_candidate,
    publish_candidate_proof,
)


def test_sandbox_config_ignores_restored_config_and_disables_host_side_effects(
    tmp_path: Path,
) -> None:
    pgdata = tmp_path / "sandbox" / "data"
    socket_dir = tmp_path / "socket"
    pgdata.mkdir(parents=True)
    socket_dir.mkdir()
    (pgdata / "postgresql.conf").write_text(
        "include='host.conf'\nshared_preload_libraries='host_library'\n"
    )

    config = _write_sandbox_config(pgdata, socket_dir, 55432, tmp_path)

    value = config.read_text()
    assert "include" not in value
    assert "host.conf" not in value
    assert "host_library" not in value
    for setting in (
        "archive_mode = 'off'",
        "ssl = 'off'",
        "logging_collector = 'off'",
        "shared_preload_libraries = ''",
        "session_preload_libraries = ''",
        "local_preload_libraries = ''",
        "primary_conninfo = ''",
    ):
        assert setting in value


def test_reconcile_removes_stale_postmaster_evidence_only_after_owner_is_dead(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    restore_root = tmp_path / "restore"
    owners = tmp_path / "restore-owners"
    partial = restore_root / ".run.partial"
    pgdata = partial / "sandbox" / "data"
    pgdata.mkdir(parents=True)
    owners.mkdir()
    (pgdata / "postmaster.pid").write_text("999999\n")
    owner = owners / "run.owner.json"
    owner.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "postgres_running",
                "partial": str(partial),
                "pid": 888888,
                "created_at": 1.0,
                "pgid": 888888,
                "deadline": 1.0,
                "sandbox_pid": 999999,
                "sandbox_created_at": 1.0,
                "sandbox_pgid": 888888,
                "sandbox_pgdata": str(pgdata),
            }
        )
    )

    def no_process(_pid: int, _created: float) -> psutil.Process | None:
        return None

    def no_group(_pgid: int) -> list[psutil.Process]:
        return []

    monkeypatch.setattr(restore_proof, "_matching_process", no_process)
    monkeypatch.setattr(restore_proof, "_group_members", no_group)

    restore_proof.reconcile_restore_runtime(tmp_path)

    assert not partial.exists()
    assert not owner.exists()


def test_prove_candidate_publishes_only_after_restore_and_live_identity_match(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    candidate = CandidateManifest(
        1,
        "20260829T000000Z",
        False,
        17,
        "ava",
        "42",
        16 * 1024 * 1024,
        1,
        "0/1000000",
        "0/2000000",
        (WalRange(1, "0/1000000", "0/2000000"),),
        BaseObject("base", 7, 1, "crc", "sha", 1, "key", "AVAPITRB1"),
        "native",
        "backup_manifest",
        "base",
        7,
        "migrations",
    )
    calls: list[str] = []
    live = LivePostgresIdentity(11, 1.0, "/live", "42", "start", "probe")

    class Reader:
        def download_exact(self, expected: RestoreObject, destination: Path) -> None:
            assert expected.object_name == "base"
            calls.append("download")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"x")

    class Executor:
        def live_identity(self) -> LivePostgresIdentity:
            calls.append("live")
            return live

        def run(
            self,
            *,
            pgdata: Path,
            wal_dir: Path,
            candidate: CandidateManifest,
            run_root: Path,
            owner_path: Path,
        ) -> DrillResult:
            assert all(
                path.is_relative_to(tmp_path) for path in (pgdata, wal_dir, run_root, owner_path)
            )
            calls.append("restore")
            return DrillResult(candidate.end_lsn, 1, 1, 1, "restored")

    class Publisher:
        def put_manifest_if_absent(
            self, *, payload: bytes, object_name: str, metadata: dict[str, str]
        ) -> RemoteObjectAck:
            assert calls[-2:] == ["restore", "live"]
            calls.append("publish")
            return RemoteObjectAck(object_name, 9, len(payload), "manifest-crc", metadata, True)

    class Process:
        pid = 1234

        @staticmethod
        def create_time() -> float:
            return 1.0

    def no_archives(_ranges: tuple[WalRange, ...], _segment_size: int) -> tuple[str, ...]:
        return ()

    def authenticate(_source: Path, *, key: bytes, expected: RestoreObject) -> dict[str, object]:
        assert key and expected.object_name == "base"
        return {}

    def extract(
        _source: Path,
        destination: Path,
        *,
        key: bytes,
        expected: RestoreObject,
        candidate_sha256: str,
        native_manifest_sha256: str,
        max_extracted_bytes: int,
    ) -> Path:
        assert key and expected.object_name == "base"
        assert candidate_sha256 and native_manifest_sha256 and max_extracted_bytes
        pgdata = destination / "data"
        pgdata.mkdir(parents=True)
        return pgdata

    monkeypatch.setattr(restore_proof, "required_archive_names", no_archives)
    monkeypatch.setattr(restore_manifest, "required_archive_names", no_archives)
    monkeypatch.setattr(restore_proof, "authenticate_base_ciphertext", authenticate)
    monkeypatch.setattr(restore_proof, "extract_authenticated_base", extract)
    monkeypatch.setattr(restore_proof.psutil, "Process", Process)
    monkeypatch.setattr(restore_proof.os, "getpgrp", lambda: 1234)

    pending = prove_candidate(
        candidate=candidate,
        root=tmp_path,
        ack_dir=tmp_path / "ack",
        key=b"k" * 32,
        reader=Reader(),
        executor=Executor(),
        budget=RestoreSpaceBudget(0, 0, 0),
    )

    assert pending.protected is True
    assert calls == ["live", "download", "restore", "live"]
    assert not (tmp_path / "protected-manifests" / f"{candidate.chain_id}.json").exists()
    protected = publish_candidate_proof(
        candidate=candidate,
        root=tmp_path,
        prefix="pitr",
        publisher=Publisher(),
    )

    assert protected.protected is True
    assert calls == ["live", "download", "restore", "live", "publish"]
    assert (tmp_path / "protected-manifests" / f"{candidate.chain_id}.json").is_file()
