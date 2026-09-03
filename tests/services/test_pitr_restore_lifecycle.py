from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from subprocess import TimeoutExpired

import psutil
import pytest
from pytest import MonkeyPatch

from services.pitr import restore_manifest, restore_postgres, restore_proof
from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
from services.pitr.checksums import CRC32C, ObjectChecksum
from services.pitr.object_store import RemoteObjectAck
from services.pitr.restore_manifest import RestoreObject
from services.pitr.restore_postgres import (
    _append_recovery_config,
    _live_identity,
    _run,
    _start_sandbox_postgres,
    _write_sandbox_config,
)
from services.pitr.restore_proof import (
    DrillResult,
    LivePostgresIdentity,
    RestoreProofError,
    RestoreSpaceBudget,
    prove_candidate,
    publish_candidate_proof,
    verify_candidate_proof,
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
        BaseObject("base", "7", 1, "crc", "crc32c", "crc", "sha", 1, "key", "AVAPITRB1"),
        "native",
        "backup_manifest",
        "base",
        "7",
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
            return RemoteObjectAck(
                object_name=object_name,
                pin_token="9",  # noqa: S106 — test fixture
                size=len(payload),
                checksum=ObjectChecksum(CRC32C, "manifest-crc"),
                metadata=metadata,
                created=True,
            )

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
    verified = verify_candidate_proof(
        candidate=candidate,
        root=tmp_path,
        ack_dir=tmp_path / "ack",
    )
    protected = publish_candidate_proof(
        candidate=candidate,
        root=tmp_path,
        prefix="pitr",
        verified=verified,
        publisher=Publisher(),
    )

    assert protected.protected is True
    assert calls == ["live", "download", "restore", "live", "publish"]
    assert (tmp_path / "protected-manifests" / f"{candidate.chain_id}.json").is_file()

    local = tmp_path / "protected-manifests" / f"{candidate.chain_id}.json"
    pending_path = tmp_path / "protected-pending" / f"{candidate.chain_id}.json"
    local.unlink()
    pending_path.write_text(protected.to_json())
    lost = False

    class LosingPublisher(Publisher):
        def put_manifest_if_absent(
            self, *, payload: bytes, object_name: str, metadata: dict[str, str]
        ) -> RemoteObjectAck:
            nonlocal lost
            ack = RemoteObjectAck(
                object_name=object_name,
                pin_token="9",  # noqa: S106 — test fixture
                size=len(payload),
                checksum=ObjectChecksum(CRC32C, "manifest-crc"),
                metadata=metadata,
                created=True,
            )
            lost = True
            return ack

    with pytest.raises(RuntimeError, match="lease lost"):
        publish_candidate_proof(
            candidate=candidate,
            root=tmp_path,
            prefix="pitr",
            verified=protected,
            publisher=LosingPublisher(),
            require_ownership=lambda: (
                (_ for _ in ()).throw(RuntimeError("lease lost")) if lost else None
            ),
        )
    assert pending_path.is_file()
    assert not local.exists()


def test_append_recovery_config_accepts_production_partial_layout(tmp_path: Path) -> None:
    """The 2026-08-30 activation died here: pgdata resolves inside
    partial/sandbox while wal_dir and socket_dir resolve inside partial, and
    the old check compared every path against pgdata.parent. The owned
    boundary is run_root, not the extracted PGDATA's parent."""
    partial = tmp_path / "restore" / ".run.partial"
    pgdata = partial / "sandbox" / "data"
    wal_dir = partial / "archive"
    socket_dir = partial / "socket"
    for directory in (pgdata, wal_dir, socket_dir):
        directory.mkdir(parents=True)

    _append_recovery_config(pgdata, wal_dir, socket_dir, "0/200", partial)

    assert (partial / "restore-allowlist.json").is_file()
    config = pgdata / "postgresql.auto.conf"
    assert config.is_file()
    assert "recovery_target_lsn = '0/200'" in config.read_text()
    assert (pgdata / "recovery.signal").is_file()


def test_append_recovery_config_rejects_path_outside_run_root(tmp_path: Path) -> None:
    partial = tmp_path / "restore" / ".run.partial"
    pgdata = partial / "sandbox" / "data"
    socket_dir = partial / "socket"
    for directory in (pgdata, socket_dir):
        directory.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(RestoreProofError, match="escaped"):
        _append_recovery_config(pgdata, outside, socket_dir, "0/200", partial)


def test_live_identity_probe_needs_no_settings_privilege() -> None:
    """PG 17 gates current_setting('data_directory') behind
    pg_read_all_settings; the worker probe must succeed on the runtime role
    with no settings-read grant (the 2026-08-30 activation's InsufficientPrivilege)."""
    import psycopg

    from shared.pg_tools import throwaway_postgres

    with throwaway_postgres() as url:
        admin = url.rsplit("/", 1)[0] + "/postgres"
        with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("CREATE ROLE viewer LOGIN")
            cur.execute("SELECT current_setting('data_directory')")
            row = cur.fetchone()
        if row is None:
            raise RuntimeError("throwaway PostgreSQL omitted its data directory")
        data_directory = str(row[0])
        viewer_url = url.replace("ava@", "viewer@", 1)

        identity = _live_identity(viewer_url, data_directory)
        pid_line = Path(data_directory, "postmaster.pid").read_text().splitlines()[0]

    assert identity.data_directory == data_directory
    assert identity.system_identifier
    assert identity.pid == int(pid_line)


def test_restore_run_token_fits_the_socket_path_budget() -> None:
    """The run-directory token must keep the sandboxed PostgreSQL's unix socket
    path under the macOS 103-byte sun_path cap for the real restore root: the
    fixed prefix (~/.ava/physical-backup/restore/, 47 chars) plus
    "/socket/.s.PGSQL.<port>" leaves 33 chars for the ".{token}.partial" name.
    The 2026-09-03 activation #7 failed exactly here (full run_id in the
    directory name); tests never caught it because tmp_path roots are short."""
    now = datetime(2026, 9, 3, 2, 12, 42, tzinfo=UTC)
    chain_ids = (
        # Activation chain: timestamp + 36-char operation uuid.
        "activation-20260902T161958Z-24e5f23a-5de2-45be-b6a8-fcd51f3642e5",
        # Scheduled-proof chain: dash-less timestamp id.
        "20260901T040728Z",
    )
    for chain_id in chain_ids:
        token = restore_proof._restore_run_token(chain_id, now)
        assert len(token) <= restore_proof._MAX_RUN_DIR_NAME_LEN - len(".partial") - 1
        assert len(f".{token}.partial") <= restore_proof._MAX_RUN_DIR_NAME_LEN
        # Deterministic and chain-distinguishable.
        assert restore_proof._restore_run_token(chain_id, now) == token
        assert now.strftime("%Y%m%dT%H%M%SZ") in token
        assert token.split("-")[-1] == chain_id.rsplit("-", 1)[-1][:6]


def test_run_error_carries_the_child_output_tail() -> None:
    """_run must not swallow the child's stderr: the 2026-09-03 activation #7
    sandbox postmaster failure was invisible because pg_ctl's stderr went to
    DEVNULL. A failing child now names its output (bounded tail)."""
    marker = "restore-command-failure-marker"
    with pytest.raises(RestoreProofError) as excinfo:
        _run(
            [
                sys.executable,
                "-c",
                f"import sys; print('{marker}', file=sys.stderr); sys.exit(3)",
            ],
            timeout=30,
        )
    assert marker in str(excinfo.value)


def test_run_error_tail_is_bounded() -> None:
    """A verbose child failure stays bounded in the error message."""
    with pytest.raises(RestoreProofError) as excinfo:
        _run(
            [
                sys.executable,
                "-c",
                "import sys; print('x' * 20000, file=sys.stderr); sys.exit(3)",
            ],
            timeout=30,
        )
    message = str(excinfo.value)
    assert len(message) < 4500
    assert message.endswith("x" * 500)


def test_start_sandbox_postgres_passes_log_and_timeout_bound(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Activation #8/#9 hang: pg_ctl -w start without -l leaves the sandbox
    postmaster holding pg_ctl's stdout pipe, so a capture_output wait never
    sees EOF (pg_ctl had already printed "server started"). The start must
    pass -l (postmaster output -> log file, pg_ctl exits on readiness) and an
    explicit -t bound mirroring the executor timeout."""
    calls: list[list[str]] = []
    log_path = tmp_path / "sandbox-postgres.log"
    pgdata = tmp_path / "data"

    def fake_run(command: list[str], *, timeout: float) -> None:
        calls.append(command)
        assert timeout == 900

    monkeypatch.setattr(restore_postgres, "_run", fake_run)
    _start_sandbox_postgres(
        Path("/pg_ctl"),
        pgdata,
        "-c config_file=/x/sandbox-postgresql.conf",
        log_path,
        900,
    )

    assert calls == [
        [
            "/pg_ctl",
            "-D",
            str(pgdata),
            "-l",
            str(log_path),
            "-t",
            "900",
            "-o",
            "-c config_file=/x/sandbox-postgresql.conf",
            "-w",
            "start",
        ]
    ]


def test_start_sandbox_postgres_attaches_log_tail_on_failure(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """pg_ctl -l points at the log for the real cause (its stderr only says
    "Examine the log output"); the run root is cleaned on failure, so the
    tail must ride with the raised error."""

    def fake_run(command: list[str], *, timeout: float) -> None:
        raise RestoreProofError("restore command exited 1: pg_ctl: could not start server")

    monkeypatch.setattr(restore_postgres, "_run", fake_run)
    log_path = tmp_path / "sandbox-postgres.log"
    log_path.write_text(
        "2026-09-03 LOG:  starting PostgreSQL\n"
        '2026-09-03 FATAL:  lock file "postmaster.pid" already exists\n'
    )

    with pytest.raises(RestoreProofError, match="already exists"):
        _start_sandbox_postgres(
            Path("/pg_ctl"), tmp_path / "data", "-c config_file=/x", log_path, 900
        )


def test_start_sandbox_postgres_reports_timeout_with_log_tail(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    def fake_run(command: list[str], *, timeout: float) -> None:
        raise TimeoutExpired(command, timeout)

    monkeypatch.setattr(restore_postgres, "_run", fake_run)
    log_path = tmp_path / "sandbox-postgres.log"
    log_path.write_text("recovery still in progress\n")

    with pytest.raises(RestoreProofError, match="recovery still in progress"):
        _start_sandbox_postgres(
            Path("/pg_ctl"), tmp_path / "data", "-c config_file=/x", log_path, 900
        )


def test_start_sandbox_postgres_no_log_file_keeps_original_error(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    def fake_run(command: list[str], *, timeout: float) -> None:
        raise RestoreProofError("restore command exited 1: pg_ctl: boom")

    monkeypatch.setattr(restore_postgres, "_run", fake_run)

    with pytest.raises(RestoreProofError, match="boom"):
        _start_sandbox_postgres(
            Path("/pg_ctl"),
            tmp_path / "data",
            "-c config_file=/x",
            tmp_path / "absent.log",
            900,
        )
