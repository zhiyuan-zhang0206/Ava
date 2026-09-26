from __future__ import annotations

import json
import os
import signal
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
from services.pitr.base_manifest import SCHEMA_VERSION, BaseObject, CandidateManifest, WalRange
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
from services.pitr.worker_process import NativeProcess
from shared import pg_tools
from shared.native_process import native_boot_id
from shared.native_process.ownership import OwnedProcess


def _native(pid: int, birth: float = 1.0) -> NativeProcess:
    boot = native_boot_id()
    assert boot is not None
    return NativeProcess(boot, OwnedProcess(pid, birth, 1))


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


def test_reconcile_retains_interrupted_postmaster_evidence_after_owner_dies(
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
                "native": _native(888888).value(),
                "pgid": 888888,
                "deadline": 1.0,
                "sandbox_native": _native(999999).value(),
                "sandbox_pgid": 999999,
                "sandbox_sid": 888888,
                "sandbox_pgdata": str(pgdata),
            }
        )
    )

    def no_process(_native: NativeProcess) -> psutil.Process | None:
        return None

    def no_group(_pgid: int) -> list[psutil.Process]:
        return []

    monkeypatch.setattr(restore_proof, "_matching_process", no_process)

    original = owner.read_bytes()
    with pytest.raises(RestoreProofError, match="native operation retirement"):
        restore_proof.reconcile_restore_runtime(tmp_path)
    assert partial.exists()
    assert owner.read_bytes() == original


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
    live = LivePostgresIdentity(_native(11), "/live", "42", "start", "probe")

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

    process = Process()
    owner = _native(process.pid)

    def capture_owner(_cls: type[NativeProcess], observed: psutil.Process) -> NativeProcess:
        # This owner is fabricated; native capture is covered by real-process tests.
        assert observed is process
        return owner

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
    monkeypatch.setattr(restore_proof.psutil, "Process", lambda: process)
    monkeypatch.setattr(NativeProcess, "capture", classmethod(capture_owner))
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
    assert identity.native.process.pid == int(pid_line)


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


def test_spawn_sandbox_postgres_inherits_the_operation_group(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """The outer operation worker retains the only process-group pin."""
    captured: dict[str, Any] = {}

    def fake_launch(argv: list[str], **kwargs: Any) -> tuple[object, object]:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return object(), object()

    monkeypatch.setattr(restore_postgres.subprocess, "Popen", fake_launch)
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
    assert "process_group" not in kwargs
    assert "start_new_session" not in kwargs
    assert isinstance(kwargs["stdout"], int)
    assert isinstance(kwargs["stderr"], int)


def test_spawn_sandbox_postgres_carries_the_start_env_fallback(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """Task #3829: a locale-less caller (launchd, a non-interactive drill
    start) must not hand the sandbox postmaster a locale-less environment —
    the spawn carries the same explicit start env as every other Postgres
    start in this codebase (`pg_start_env`, Task #3754); on macOS the missing
    locale is the "postmaster became multithreaded during startup" abort."""
    captured: dict[str, Any] = {}

    def fake_launch(argv: list[str], **kwargs: Any) -> tuple[object, object]:
        captured["env"] = kwargs.get("env")
        return object(), object()

    monkeypatch.setattr(restore_postgres.subprocess, "Popen", fake_launch)
    monkeypatch.setattr(pg_tools, "is_macos", lambda: True)
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LANG", raising=False)

    _spawn_sandbox_postgres(
        Path("/pg/postgres"),
        tmp_path / "data",
        tmp_path / "sandbox-postgresql.conf",
        tmp_path / "sandbox-postgres.log",
    )

    env = captured["env"]
    assert env["LC_ALL"] == "en_US.UTF-8"
    assert env["PATH"] == os.environ["PATH"], "the caller's env is inherited otherwise"


def test_wait_for_sandbox_identity_raises_crash_with_log_tail(
    tmp_path: Path,
) -> None:
    identity = SandboxPostgresIdentity(_native(999999), 999999, os.getsid(0), "/data")

    log_path = tmp_path / "sandbox-postgres.log"
    log_path.write_text("FATAL:  could not open file\n")
    with pytest.raises(RestoreProofError, match=r"exited.*could not open file"):
        _wait_for_sandbox_identity(
            identity,
            tmp_path / "data",
            log_path,
            30,
        )


def test_wait_for_sandbox_identity_returns_once_pid_file_exists(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    identity = SandboxPostgresIdentity(
        NativeProcess.capture(psutil.Process()), os.getpgrp(), os.getsid(0), "/data"
    )

    def fake_identity(_pgdata: Path) -> SandboxPostgresIdentity:
        return identity

    monkeypatch.setattr(restore_postgres, "_sandbox_identity", fake_identity)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "postmaster.pid").write_text("123\n")

    assert (
        _wait_for_sandbox_identity(
            identity,
            tmp_path / "data",
            tmp_path / "sandbox-postgres.log",
            30,
        )
        is identity
    )


def test_wait_for_sandbox_identity_times_out(tmp_path: Path) -> None:
    identity = SandboxPostgresIdentity(_native(999999), 999999, os.getsid(0), "/data")

    (tmp_path / "data").mkdir()
    with pytest.raises(RestoreProofError, match="never wrote its pid file"):
        _wait_for_sandbox_identity(
            identity,
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

    identity = SandboxPostgresIdentity(_native(999999), 999999, os.getsid(0), "/data")

    log_path = tmp_path / "sandbox-postgres.log"
    log_path.write_text("replay stalled then died\n")
    with pytest.raises(RestoreProofError, match=r"exited before promotion.*stalled"):
        executor._wait_for_promotion(
            tmp_path / "socket",
            54321,
            _dummy_candidate(),
            identity,
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


@pytest.mark.parametrize("failure", ["smoke", "closure", "capture"])
def test_executor_reaps_sandbox_only_after_proven_closure(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: MonkeyPatch, failure: str
) -> None:
    """A failed restore reaps after closure, while unknown closure retains
    the leader and durable ownership evidence for the worker session owner."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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
            "native": NativeProcess.capture(psutil.Process()).value(),
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

    class LiveProbe:
        data_directory = "/unused"

    def fake_verify(_command: list[str], *, timeout: float) -> None:
        return None

    def fake_spawn(
        _postgres: Path, _pgdata: Path, _config_file: Path, _log_path: Path
    ) -> subprocess.Popen[bytes]:
        return child

    def fake_wait_identity(
        _identity: SandboxPostgresIdentity,
        _pgdata: Path,
        _log_path: Path,
        _timeout: int,
    ) -> SandboxPostgresIdentity:
        return SandboxPostgresIdentity(
            NativeProcess.capture(psutil.Process(child.pid)),
            os.getpgid(child.pid),
            os.getsid(child.pid),
            str(pgdata.resolve()),
        )

    def fake_wait_promotion(
        _socket_dir: Path,
        _port: int,
        _candidate: CandidateManifest,
        _identity: SandboxPostgresIdentity | None = None,
        _log_path: Path | None = None,
    ) -> str:
        return "0/2000000"

    def fake_smoke(_socket_dir: Path, _port: int, _candidate: CandidateManifest) -> str:
        raise RestoreProofError("restored migration set differs")

    def fake_stop(
        _pgdata: Path, _identity: SandboxPostgresIdentity, _process: subprocess.Popen[bytes]
    ) -> None:
        # SIGKILL (SIG_IGN-inherited sessions ignore SIGTERM): dead child left
        # unreaped, as the real stop path does.
        os.kill(child.pid, signal.SIGKILL)
        if failure == "closure":
            raise RestoreProofError("sandbox closure unresolved")
        child.wait(timeout=5)

    monkeypatch.setattr(restore_postgres, "_run", fake_verify)
    monkeypatch.setattr(restore_postgres, "_spawn_sandbox_postgres", fake_spawn)
    monkeypatch.setattr(restore_postgres, "_wait_for_sandbox_identity", fake_wait_identity)
    monkeypatch.setattr(executor, "live_identity", LiveProbe)
    monkeypatch.setattr(executor, "_wait_for_promotion", fake_wait_promotion)
    monkeypatch.setattr(executor, "_smoke", fake_smoke)
    monkeypatch.setattr(executor, "_stop", fake_stop)
    if failure == "capture":

        def denied(_process: subprocess.Popen[str], _data: Path) -> SandboxPostgresIdentity:
            raise psutil.AccessDenied(child.pid)

        monkeypatch.setattr(restore_postgres, "_capture_sandbox", denied)
    try:
        error_type = psutil.AccessDenied if failure == "capture" else RestoreProofError
        with pytest.raises(error_type) as caught:
            executor.run(
                pgdata=pgdata,
                wal_dir=run_root / "archive",
                candidate=_dummy_candidate(),
                run_root=run_root,
                owner_path=owner,
            )
        if failure == "closure":
            assert "restored migration set differs" in str(caught.value) and (
                "sandbox closure unresolved" in " ".join(caught.value.__notes__)
            )
        if failure in {"capture", "closure"}:
            assert child.returncode is None and psutil.Process(child.pid).create_time() > 0
            expected_state = "postgres_starting" if failure == "capture" else "postgres_running"
            assert json.loads(owner.read_text())["state"] == expected_state
        else:
            assert child.returncode is not None
            with pytest.raises(psutil.NoSuchProcess):
                psutil.Process(child.pid)
    finally:
        if failure == "capture":
            child.kill()
        child.wait(timeout=10)


def test_smoke_probe_accepts_a_bigint_identifier_and_a_dropped_anchor_table() -> None:
    """The restore-proof smoke, exercised here against a real cluster: the
    system identifier arrives as a bigint while the manifest froze it as text,
    and the anchor set must read a table a migration dropped (events,
    2026-08-29) as absent evidence rather than a mismatch — the two defects
    that blocked the first live execution of this probe."""
    import getpass
    import hashlib
    from urllib.parse import urlsplit

    import psycopg
    from psycopg import sql

    from shared.pg_tools import throwaway_postgres

    with throwaway_postgres() as url:
        with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
            # The sandbox cluster is a physical copy of the live one, whose
            # bootstrap superuser is the installing OS user; the probe dials
            # with no user name, so libpq falls back to that role.
            cur.execute(
                sql.SQL("CREATE ROLE {} LOGIN SUPERUSER").format(sql.Identifier(getpass.getuser()))
            )
            cur.execute("CREATE TABLE schema_migrations (name text PRIMARY KEY)")
            cur.execute(
                "INSERT INTO schema_migrations (name) VALUES "
                "('20260101T000000_alpha'), ('20260102T000000_beta')"
            )
            cur.execute("CREATE TABLE agents_meta (id bigint PRIMARY KEY, title text)")
            cur.execute("INSERT INTO agents_meta VALUES (1, 'agent')")
            cur.execute("CREATE TABLE checkpoints (thread_id text, checkpoint_id text)")
            cur.execute("INSERT INTO checkpoints VALUES ('thread-1', 'checkpoint-1')")
            cur.execute("SELECT name FROM schema_migrations ORDER BY name")
            names = [str(row[0]) for row in cur.fetchall()]
            cur.execute("SELECT system_identifier FROM pg_control_system()")
            identity_row = cur.fetchone()
            assert identity_row is not None
            assert isinstance(identity_row[0], int)
            cur.execute("SHOW unix_socket_directories")
            socket_row = cur.fetchone()
            assert socket_row is not None
        candidate = CandidateManifest(
            SCHEMA_VERSION,
            "activation-test",
            False,
            17,
            "ava_citest",
            str(identity_row[0]),
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
            hashlib.sha256("\n".join(names).encode()).hexdigest(),
        )
        port = urlsplit(url).port
        assert port is not None
        socket_dir = Path(str(socket_row[0]))

        fingerprint = IsolatedPostgresRestoreExecutor._smoke(socket_dir, port, candidate)
        assert fingerprint == IsolatedPostgresRestoreExecutor._smoke(socket_dir, port, candidate)

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            samples = restore_postgres._smoke_samples(cur)
    assert "agents_meta" in samples
    assert "checkpoints" in samples
    assert "absent:events" in samples
