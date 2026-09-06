from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
import pytest
from pytest import MonkeyPatch

from services.pitr import restore_manifest, restore_postgres, restore_proof
from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
from services.pitr.checksums import CRC32C, ObjectChecksum
from services.pitr.object_store import RemoteObjectAck
from services.pitr.restore_manifest import RestoreObject
from services.pitr.restore_postgres import (
    IsolatedPostgresRestoreExecutor,
    SandboxPostgresIdentity,
    _append_recovery_config,
    _live_identity,
    _run,
    _spawn_sandbox_postgres,
    _wait_for_sandbox_identity,
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


def test_spawn_sandbox_postgres_runs_postgres_directly_in_our_group(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Activation #10 surfaced the group tripwire: pg_ctl setsid()s the
    postmaster into its own session, but the restore design reaps a whole run
    by signalling the worker's process group. The sandbox must be exec'd
    directly so it stays in our group, with output in the run-root log."""
    captured: dict[str, Any] = {}

    class FakePopen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            captured["argv"] = argv
            captured["kwargs"] = kwargs

    monkeypatch.setattr(restore_postgres.subprocess, "Popen", FakePopen)
    log_path = tmp_path / "sandbox-postgres.log"

    _spawn_sandbox_postgres(
        Path("/pg/postgres"),
        tmp_path / "data",
        tmp_path / "sandbox-postgresql.conf",
        log_path,
    )

    assert captured["argv"] == [
        "/pg/postgres",
        "-D",
        str(tmp_path / "data"),
        "-c",
        f"config_file={tmp_path / 'sandbox-postgresql.conf'}",
    ]
    kwargs = captured["kwargs"]
    assert kwargs["stdin"] is restore_postgres.subprocess.DEVNULL
    assert isinstance(kwargs["stdout"], int)
    assert isinstance(kwargs["stderr"], int)


def test_wait_for_sandbox_identity_raises_crash_with_log_tail(
    tmp_path: Path,
) -> None:
    class DeadProcess:
        returncode = 1

        def poll(self) -> int:
            return 1

    log_path = tmp_path / "sandbox-postgres.log"
    log_path.write_text("FATAL:  could not open file\n")
    with pytest.raises(RestoreProofError, match=r"exited 1.*could not open file"):
        _wait_for_sandbox_identity(
            DeadProcess(),  # type: ignore[arg-type]
            tmp_path / "data",
            log_path,
            30,
        )


def test_wait_for_sandbox_identity_returns_once_pid_file_exists(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    class LiveProcess:
        def poll(self) -> None:
            return None

    identity = SandboxPostgresIdentity(11, 1.0, 11, "/data")

    def fake_identity(_pgdata: Path) -> SandboxPostgresIdentity:
        return identity

    monkeypatch.setattr(restore_postgres, "_sandbox_identity", fake_identity)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "postmaster.pid").write_text("123\n")

    assert (
        _wait_for_sandbox_identity(
            LiveProcess(),  # type: ignore[arg-type]
            tmp_path / "data",
            tmp_path / "sandbox-postgres.log",
            30,
        )
        is identity
    )


def test_wait_for_sandbox_identity_times_out(tmp_path: Path) -> None:
    class LiveProcess:
        def poll(self) -> None:
            return None

    (tmp_path / "data").mkdir()
    with pytest.raises(RestoreProofError, match="never wrote its pid file"):
        _wait_for_sandbox_identity(
            LiveProcess(),  # type: ignore[arg-type]
            tmp_path / "data",
            tmp_path / "sandbox-postgres.log",
            0,
        )


def test_wait_for_promotion_raises_on_postmaster_crash(tmp_path: Path) -> None:
    """A sandbox that dies mid-recovery must fail fast with its log tail, not
    poll connections until the 900 s deadline."""
    executor = IsolatedPostgresRestoreExecutor(
        live_db_url="postgresql://unused",
        data_directory="/unused",
        pg_ctl=Path("/pg/pg_ctl"),
        pg_verifybackup=Path("/pg/pg_verifybackup"),
        timeout_seconds=900,
    )

    class DeadProcess:
        returncode = 6

        def poll(self) -> int:
            return 6

    log_path = tmp_path / "sandbox-postgres.log"
    log_path.write_text("replay stalled then died\n")
    with pytest.raises(RestoreProofError, match=r"exited 6 before promotion.*stalled"):
        executor._wait_for_promotion(
            tmp_path / "socket",
            54321,
            _dummy_candidate(),
            DeadProcess(),  # type: ignore[arg-type]
            log_path,
        )


def _dummy_candidate() -> CandidateManifest:
    return CandidateManifest(
        1,
        "20260904T000000Z",
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


def _dead_child() -> tuple[subprocess.Popen[str], int, float, int]:
    """A real child that has exited but is not yet reaped (a zombie).

    The caller must popen.wait() in a finally to reap it."""
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    probe = psutil.Process(process.pid)
    created_at = probe.create_time()
    pgid = os.getpgid(process.pid)
    process.terminate()
    deadline = time.monotonic() + 10
    while probe.status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
        time.sleep(0.01)
    assert probe.status() == psutil.STATUS_ZOMBIE
    return process, process.pid, created_at, pgid


def test_matching_process_treats_a_zombie_as_not_live() -> None:
    """Activation #12: a stopped-but-unreaped sandbox postmaster kept passing
    the create_time probe, so cleanup refused to remove a dead restore and
    masked the real failure. A zombie runs nothing and is not live."""
    process, pid, created_at, _pgid = _dead_child()
    try:
        assert restore_proof._matching_process(pid, created_at) is None
        assert not restore_proof._sandbox_is_live(
            {"sandbox_pid": pid, "sandbox_created_at": created_at}
        )
    finally:
        process.wait(timeout=10)


def test_matching_sandbox_treats_a_zombie_as_not_live() -> None:
    process, pid, created_at, pgid = _dead_child()
    try:
        identity = SandboxPostgresIdentity(pid, created_at, pgid, "/data")
        assert restore_postgres._matching_sandbox(identity) is None
    finally:
        process.wait(timeout=10)


def test_group_members_excludes_zombies() -> None:
    process, pid, _created_at, pgid = _dead_child()
    try:
        assert all(member.pid != pid for member in restore_proof._group_members(pgid))
    finally:
        process.wait(timeout=10)


def test_executor_run_reaps_the_sandbox_postmaster_on_failure(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """The direct-exec postmaster is the worker's child; the stop path never
    reaps it (a zombie fails the pgid identity probe), so run()'s finally must
    reap the Popen itself — otherwise cleanup still sees a "live" restore and
    masks the real failure (activation #12)."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    # Mirror the production partial layout: everything the sandbox touches
    # lives under the run root (recovery config paths are root-checked).
    run_root = tmp_path / "run"
    (run_root / "archive").mkdir(parents=True)
    pgdata = run_root / "sandbox" / "data"
    pgdata.mkdir(parents=True)
    owner = tmp_path / "owner.json"
    restore_proof._atomic_owner(
        owner,
        {
            "schema_version": 1,
            "state": "spawning",
            "run_id": "test-run",
            "partial": str(tmp_path / ".test.partial"),
            "pid": os.getpid(),
            "created_at": psutil.Process().create_time(),
            "pgid": os.getpgrp(),
            "deadline": time.time() + 100,
        },
    )
    executor = IsolatedPostgresRestoreExecutor(
        live_db_url="postgresql://unused",
        data_directory="/unused",
        pg_ctl=Path("/pg/pg_ctl"),
        pg_verifybackup=Path("/pg/pg_verifybackup"),
        timeout_seconds=900,
    )
    created_at = psutil.Process(child.pid).create_time()

    class LiveProbe:
        data_directory = "/unused"

    def fake_verify(_command: list[str], *, timeout: float) -> None:
        return None

    def fake_spawn(
        _postgres: Path, _pgdata: Path, _config_file: Path, _log_path: Path
    ) -> subprocess.Popen[str]:
        return child

    def fake_wait_identity(
        _process: subprocess.Popen[str],
        _pgdata: Path,
        _log_path: Path,
        _timeout: int,
    ) -> SandboxPostgresIdentity:
        return SandboxPostgresIdentity(child.pid, created_at, os.getpgrp(), str(pgdata.resolve()))

    def fake_wait_promotion(
        _socket_dir: Path,
        _port: int,
        _candidate: CandidateManifest,
        _process: subprocess.Popen[str] | None = None,
        _log_path: Path | None = None,
    ) -> str:
        return "0/2000000"

    def fake_smoke(_socket_dir: Path, _port: int, _candidate: CandidateManifest) -> str:
        raise RestoreProofError("restored migration set differs")

    def fake_stop(_pgdata: Path, _identity: SandboxPostgresIdentity) -> None:
        child.terminate()  # dead child left unreaped, as the real stop path does

    monkeypatch.setattr(restore_postgres, "_run", fake_verify)
    monkeypatch.setattr(restore_postgres, "_spawn_sandbox_postgres", fake_spawn)
    monkeypatch.setattr(restore_postgres, "_wait_for_sandbox_identity", fake_wait_identity)
    monkeypatch.setattr(executor, "live_identity", LiveProbe)
    monkeypatch.setattr(executor, "_wait_for_promotion", fake_wait_promotion)
    monkeypatch.setattr(executor, "_smoke", fake_smoke)
    monkeypatch.setattr(executor, "_stop", fake_stop)
    try:
        with pytest.raises(RestoreProofError, match="migration set differs"):
            executor.run(
                pgdata=pgdata,
                wal_dir=run_root / "archive",
                candidate=_dummy_candidate(),
                run_root=run_root,
                owner_path=owner,
            )
        assert child.poll() is not None
        with pytest.raises(psutil.NoSuchProcess):
            psutil.Process(child.pid)
    finally:
        child.wait(timeout=10)
