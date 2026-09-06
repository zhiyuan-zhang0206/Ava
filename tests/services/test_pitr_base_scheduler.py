from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import psutil
import pytest

import services.pitr.base_operation_runtime as restore_runtime
import services.pitr.base_scheduler_daemon as daemon
from services.pitr import retention_scheduler
from services.pitr.activation_state import ActivationRecord, write_record
from services.pitr.base_manifest import BaseObject, CandidateManifest, WalRange
from services.pitr.base_scheduler_daemon import BaseCandidateState, _components, is_due
from services.pitr.restore_manifest import (
    ProtectedManifest,
    RestoreObject,
    RestoreProof,
    candidate_sha256,
    required_archive_names,
)
from services.pitr.restore_proof import RestoreSpaceBudget
from services.pitr.retention_planner import DryRunResult
from services.pitr.retention_scheduler import RetentionDryRunState
from services.pitr.retention_scheduler import health_component as retention_health_component
from shared import telemetry
from shared.platform import LockTimeoutError
from shared.process_env import restricted_process_env


def _candidate(chain_id: str) -> CandidateManifest:
    return CandidateManifest(
        schema_version=1,
        chain_id=chain_id,
        protected=False,
        postgres_major=17,
        database_name="ava",
        system_identifier="1",
        wal_segment_size=16 * 1024 * 1024,
        timeline=1,
        start_lsn="0/100",
        end_lsn="0/200",
        wal_ranges=(WalRange(1, "0/100", "0/200"),),
        base_object=BaseObject(
            "base", "1", 10, "crc", "crc32c", "crc", "sha", 5, "key", "AVAPITRB1"
        ),
        native_manifest_sha256="manifest",
        native_manifest_member_path="backup_manifest",
        native_manifest_container_object_name="base",
        native_manifest_container_pin_token="1",  # noqa: S106 — test fixture
        migration_set_sha256="migrations",
    )


def _required_wal(candidate: CandidateManifest) -> tuple[RestoreObject, ...]:
    return tuple(
        RestoreObject(name, "wal", str(2 + index), 10, "crc32c", "crc", ())
        for index, name in enumerate(
            required_archive_names(candidate.wal_ranges, candidate.wal_segment_size)
        )
    )


def test_restore_worker_exec_import_boundary_has_no_publisher_or_settings(tmp_path: Path) -> None:
    uploader = tmp_path / "uploader.json"
    uploader.write_text("publisher-only")
    inputs = daemon._RestoreWorkerInput(
        candidate_json=_candidate("viewer-only").to_json(),
        root=tmp_path,
        ack_dir=tmp_path / "ack",
        key_path=tmp_path / "backup.key",
        backend="gcs",
        store_args=(
            ("project", "project"),
            ("bucket", "bucket"),
            ("viewer_credentials", str(tmp_path / "viewer.json")),
        ),
        budget=RestoreSpaceBudget(0, 0, 0),
        live_db_url="postgresql://viewer@127.0.0.1:5433/ava",
        data_directory="/live/data",
        pg_ctl=Path("/usr/bin/true"),
        pg_verifybackup=Path("/usr/bin/true"),
    )
    assert str(uploader) not in repr(inputs)
    environment = restricted_process_env()
    assert not any(name.startswith(("AVA_", "GOOGLE_", "PG")) for name in environment)
    script = (
        "import sys; import services.pitr.restore_worker; "
        "forbidden={'shared.config','services.pitr.restore_publish_store'}; "
        "raise SystemExit(1 if forbidden & set(sys.modules) else 0)"
    )
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        check=False,
    )
    assert completed.returncode == 0


def test_restore_worker_store_args_validate_against_the_backend_constructor(
    tmp_path: Path,
) -> None:
    from services.pitr.restore_worker import _construct_group
    from services.pitr.store_factory import get_group_constructor_named

    gcs = _construct_group(
        get_group_constructor_named("gcs"),
        {
            "project": "p",
            "bucket": "b",
            "viewer_credentials": str(tmp_path / "viewer.json"),
        },
    )
    assert gcs.generation_pinned_object_reader is not None
    baidu = _construct_group(
        get_group_constructor_named("baidu"),
        {
            "app_root": "/apps/ava/ava-pitr",
            "prefix": "ava-pitr",
            "credentials_file": str(tmp_path / "creds.json"),
            "token_file": str(tmp_path / "token.json"),
        },
    )
    assert baidu.generation_pinned_object_reader is not None
    with pytest.raises(ValueError, match="unknown"):
        _construct_group(
            get_group_constructor_named("gcs"),
            {"project": "p", "bucket": "b", "nope": "x"},
        )
    with pytest.raises(ValueError, match="required"):
        _construct_group(get_group_constructor_named("gcs"), {"project": "p"})
    cos_group = _construct_group(
        get_group_constructor_named("cos"),
        {
            "bucket": "ava-pitr-1250000000",
            "region": "ap-guangzhou",
            "credentials_file": str(tmp_path / "cos.json"),
            "prefix": "ava-pitr",
        },
    )
    assert cos_group.generation_pinned_object_reader is not None


def test_restore_worker_input_builds_baidu_store_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.pitr import base_operation_runtime as restore_runtime
    from shared.config import settings

    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "baidu")
    monkeypatch.setattr(settings.physical_backup, "pitr_restore_proof_enabled", True)
    monkeypatch.setattr(settings.physical_backup, "pitr_backup_key_file", tmp_path / "backup.key")
    monkeypatch.setattr(
        settings.physical_backup, "pitr_baidu_credentials_file", tmp_path / "creds.json"
    )
    monkeypatch.setattr(settings.physical_backup, "pitr_baidu_token_file", tmp_path / "token.json")
    monkeypatch.setattr(restore_runtime, "direct_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(restore_runtime, "live_data_directory", lambda: "/live/data")

    def fake_pg_tool(_name: str) -> Path:
        return Path("/usr/bin/true")

    monkeypatch.setattr(restore_runtime, "pg_tool", fake_pg_tool)

    inputs = restore_runtime.input_for(_candidate("baidu-proof"))

    assert inputs.backend == "baidu"
    assert set(dict(inputs.store_args)) == {"app_root", "prefix", "credentials_file", "token_file"}
    assert dict(inputs.store_args)["credentials_file"] == str(tmp_path / "creds.json")


def test_restore_worker_input_builds_cos_store_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from services.pitr import base_operation_runtime as restore_runtime
    from shared.config import settings

    monkeypatch.setattr(settings.physical_backup, "pitr_store_backend", "cos")
    monkeypatch.setattr(settings.physical_backup, "pitr_restore_proof_enabled", True)
    monkeypatch.setattr(settings.physical_backup, "pitr_backup_key_file", tmp_path / "backup.key")
    monkeypatch.setattr(
        settings.physical_backup, "pitr_cos_credentials_file", tmp_path / "cos.json"
    )
    monkeypatch.setattr(settings.physical_backup, "pitr_cos_bucket", "ava-pitr-1250000000")
    monkeypatch.setattr(settings.physical_backup, "pitr_cos_region", "ap-guangzhou")
    monkeypatch.setattr(restore_runtime, "direct_db_url", lambda: "postgresql://x")
    monkeypatch.setattr(restore_runtime, "live_data_directory", lambda: "/live/data")

    def fake_pg_tool(_name: str) -> Path:
        return Path("/usr/bin/true")

    monkeypatch.setattr(restore_runtime, "pg_tool", fake_pg_tool)

    inputs = restore_runtime.input_for(_candidate("cos-proof"))

    assert inputs.backend == "cos"
    assert set(dict(inputs.store_args)) == {
        "bucket",
        "region",
        "credentials_file",
        "prefix",
    }
    assert dict(inputs.store_args)["region"] == "ap-guangzhou"


_LEGACY_CANDIDATE_JSON = (
    '{"base_object":{"ciphertext_crc32c":"viqqbw==","ciphertext_size":4101269456,'
    '"encryption_format":"AVAPITRB1","generation":1788085003231815,'
    '"key_id":"ava-pitr-backup-key-prod",'
    '"object_name":"ava-pitr/base/activation-20260830T043835Z-c1cfa2ee-de51-4d9e-ba5b-6e31d97f1c40/'
    '358fa8fd6b547520bfe14f134e1420aa683e2a3393575ebe5c07cbf7320ea2ac/base.tar.zst.enc",'
    '"source_sha256":"358fa8fd6b547520bfe14f134e1420aa683e2a3393575ebe5c07cbf7320ea2ac",'
    '"source_size":6319665156},'
    '"chain_id":"activation-20260830T043835Z-c1cfa2ee-de51-4d9e-ba5b-6e31d97f1c40",'
    '"database_name":"ava_main","end_lsn":"A4/89EC6820",'
    '"migration_set_sha256":"63124a552737c95e0296cd29a5247cec07c1014d9eb474ea2d78116c73849f2e",'
    '"native_manifest_container_generation":1788085003231815,'
    '"native_manifest_container_object_name":'
    '"ava-pitr/base/activation-20260830T043835Z-c1cfa2ee-de51-4d9e-ba5b-6e31d97f1c40/'
    '358fa8fd6b547520bfe14f134e1420aa683e2a3393575ebe5c07cbf7320ea2ac/base.tar.zst.enc",'
    '"native_manifest_member_path":"backup_manifest",'
    '"native_manifest_sha256":"5ee47ac3e20907e70894bf2761395256a78b94c116efe11f86ec26adff2153d2",'
    '"postgres_major":17,"protected":false,"schema_version":1,'
    '"start_lsn":"A4/7FC179B0","system_identifier":"7656686487711429617",'
    '"timeline":1,'
    '"wal_ranges":[{"end_lsn":"A4/89EC6820","start_lsn":"A4/7FC179B0","timeline":1}],'
    '"wal_segment_size":16777216}'
)

_LEGACY_WEEKLY_CANDIDATE_JSON = _LEGACY_CANDIDATE_JSON.replace(
    "activation-20260830T043835Z-c1cfa2ee-de51-4d9e-ba5b-6e31d97f1c40",
    "20260831T043835Z",
)


def _write_legacy_manifests(root: Path) -> Path:
    manifests = root / "base-manifests"
    manifests.mkdir(parents=True)
    (manifests / "20260831T043835Z.candidate.json").write_text(_LEGACY_WEEKLY_CANDIDATE_JSON)
    (
        manifests
        / "activation-20260830T043835Z-c1cfa2ee-de51-4d9e-ba5b-6e31d97f1c40.candidate.json"
    ).write_text(_LEGACY_CANDIDATE_JSON)
    return root


def test_legacy_manifest_daemon_paths_parse_without_crashing(
    tmp_path: Path,
) -> None:
    """QA #1131 P1: the pre-abstraction candidate shape on disk must keep
    the four boot/loop paths of the base-candidate daemon alive."""
    from datetime import UTC, datetime

    root = _write_legacy_manifests(tmp_path)
    # Path 1: boot-time scheduling gate, evaluated in the historical fixture's week.
    fixture_week = datetime(2026, 8, 31, 12, tzinfo=UTC)
    assert not daemon.is_due(fixture_week, root / "base-manifests")
    # Path 2: boot-time last-success read.
    assert daemon._last_durable_success(root / "base-manifests") is not None
    # Path 3: loop candidate scan.
    assert daemon._candidate_manifests(root / "base-manifests")
    # Path 4: pending-restore scan (the weekly candidate has no protected proof).
    assert daemon._pending_restore_candidate(root) is not None


def test_restricted_restore_group_reaps_orphan_descendant() -> None:
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)']);"
        "time.sleep(60)"
    )
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", script], start_new_session=True, text=True
    )
    created_at = psutil.Process(process.pid).create_time()
    deadline = time.monotonic() + 10
    while len(daemon._group_members(process.pid)) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(daemon._group_members(process.pid)) >= 2

    daemon._reap_restore_subprocess_group(process, created_at)

    assert daemon._group_members(process.pid) == []


def test_authoritative_verify_precedes_publisher_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    constructed = False

    def reject_mismatched_ack(**_kwargs: object) -> None:
        raise ValueError("ACK generation mismatch")

    class Publisher:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal constructed
            constructed = True

    class Group:
        @staticmethod
        def protected_manifest_publisher() -> Publisher:
            return Publisher()

    def group() -> Group:
        return Group()

    monkeypatch.setattr(restore_runtime, "verify_candidate_proof", reject_mismatched_ack)
    monkeypatch.setattr(restore_runtime, "get_store_group", group)

    with pytest.raises(ValueError, match="ACK generation mismatch"):
        restore_runtime.verify_then_construct_publisher(
            candidate=_candidate("verify-first"),
            root=tmp_path,
            ack_dir=tmp_path / "ack",
        )

    assert constructed is False


def _blocking_worker(
    started: Path,
    stopped: Path,
    stop: daemon.StopSignal,
    _output: daemon._WorkerQueue,
) -> None:
    started.write_text(str(os.getpid()))
    stop.wait()
    stopped.write_text("stopped")


def _noncooperative_worker(
    started: Path,
    armed: Path,
    late: Path,
    _stop: daemon.StopSignal,
    _output: daemon._WorkerQueue,
) -> None:
    script = (
        "import signal,subprocess,sys,time\n"
        f"late={str(late)!r}\n"
        "def spawn_late(*_args):\n"
        " p=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])\n"
        " open(late,'w').write(str(p.pid))\n"
        "signal.signal(signal.SIGTERM,spawn_late)\n"
        f"open({str(armed)!r},'w').write('armed')\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script])  # noqa: S603
    while not armed.exists():
        time.sleep(0.01)
    started.write_text(f"{os.getpid()} {child.pid}")
    time.sleep(60)


async def _wait_for_path(path: Path, *, timeout_s: float = 10) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert path.exists(), f"worker did not publish {path.name} before the deadline"


async def _assert_tree_gone(pids: list[int], timeout_s: float = 5.0) -> None:
    # After SIGKILL, dead processes can remain zombies until their new parent
    # reaps them, and psutil.pid_exists() still reports those entries. Assert
    # no live members, not that every process-table entry vanished (same
    # discipline as tests/agent/test_exec_subprocess.py::_assert_tree_gone).
    deadline = time.monotonic() + timeout_s
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        for pid in list(remaining):
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    remaining.discard(pid)
            except psutil.NoSuchProcess:
                remaining.discard(pid)
        if remaining:
            await asyncio.sleep(0.05)
    assert not remaining, f"process(es) still alive after forced shutdown: {sorted(remaining)}"


def test_due_uses_durable_candidate_after_restart(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 4, tzinfo=UTC)  # Sunday after the weekly window.
    assert is_due(now, tmp_path)
    (tmp_path / "20260830T030000Z.candidate.json").write_text(
        _candidate("20260830T030000Z").to_json()
    )
    assert not is_due(now, tmp_path)


def test_activation_candidate_never_satisfies_weekly_due(tmp_path: Path) -> None:
    now = datetime(2026, 8, 30, 4, tzinfo=UTC)
    chain = "activation-20260830T030000Z-00000000-0000-0000-0000-000000000001"
    (tmp_path / f"{chain}.candidate.json").write_text(_candidate(chain).to_json())
    assert is_due(now, tmp_path)


def test_weekly_restore_selector_never_adopts_activation_candidate(tmp_path: Path) -> None:
    manifests = tmp_path / "base-manifests"
    manifests.mkdir()
    activation_candidate = _candidate("activation-20260830T030000Z-op")
    weekly = _candidate("20260830T040000Z")
    (manifests / "activation.candidate.json").write_text(activation_candidate.to_json())
    (manifests / "weekly.candidate.json").write_text(weekly.to_json())
    assert daemon._pending_restore_candidate(tmp_path) == weekly


def test_hot_publish_updates_chain_identity() -> None:
    state = BaseCandidateState(restore_error="old")
    candidate = _candidate("20260830T040000Z")
    daemon._record_protected(state, candidate)
    assert state.last_protected is not None
    assert state.last_protected_chain == candidate.chain_id
    assert state.restore_error is None


def test_scheduler_ownership_rechecks_activation_after_lock_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "ava_home", lambda: tmp_path)
    write_record(tmp_path, ActivationRecord.start(operation_id="op-1", origin="cli"))

    with (
        pytest.raises(RuntimeError, match="activation owns base/restore selection"),
        daemon._claim_scheduler_ownership(),
    ):
        pytest.fail("scheduler entered an activation-owned critical section")


def test_scheduler_ownership_lock_excludes_the_opposite_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "ava_home", lambda: tmp_path)

    with (
        daemon._claim_scheduler_ownership(),
        pytest.raises(LockTimeoutError),
        daemon._claim_scheduler_ownership(),
    ):
        pytest.fail("two controllers owned candidate selection concurrently")


def test_health_surfaces_corrupt_activation_state_without_throwing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    operation = tmp_path / "physical-backup" / "activation" / "operation.json"
    operation.parent.mkdir(parents=True)
    operation.write_text("{corrupt")
    monkeypatch.setattr("services.pitr.activation_runtime.ava_home", lambda: tmp_path)
    components = daemon._components(BaseCandidateState())
    activation_component = next(item for item in components if item["name"] == "pitr_activation")
    assert activation_component["status"] == "degraded"
    assert activation_component["progress"] == "unknown"
    assert activation_component["gate_readiness"] is False


def test_health_never_calls_a_candidate_protected() -> None:
    components = _components(BaseCandidateState(running=True))
    assert components[0]["protected"] is False


def test_cold_start_health_recovers_real_durable_protected_manifest(tmp_path: Path) -> None:
    candidate = _candidate("20260830T030000Z")
    archive_name = "000000010000000000000000"
    protected = ProtectedManifest(
        schema_version=1,
        protected=True,
        chain_id=candidate.chain_id,
        candidate_sha256=candidate_sha256(candidate),
        candidate=candidate,
        base=RestoreObject("base", "base", "1", 10, "crc32c", "crc", ()),
        wal=(RestoreObject(archive_name, "wal", "2", 10, "crc32c", "crc", ()),),
        target_lsn="0/200",
        wal_segment_size=candidate.wal_segment_size,
        proof=RestoreProof(
            "run",
            "2026-08-30T03:00:00+00:00",
            "2026-08-30T03:01:00+00:00",
            "0/200",
            "0/200",
            123,
            "live",
            "verify",
            1.0,
            1.0,
            1.0,
            20,
            "restored",
        ),
    )
    root = tmp_path / "physical-backup"
    manifests = root / "protected-manifests"
    manifests.mkdir(parents=True)
    (manifests / f"{candidate.chain_id}.json").write_text(protected.to_json())

    last_protected, chain_id, error = daemon._last_durable_protected(root)
    assert error is None
    components = _components(
        BaseCandidateState(last_protected=last_protected, last_protected_chain=chain_id)
    )
    restore = next(item for item in components if item["name"] == "pitr_restore_proof")
    assert restore["protected"] is True
    assert restore["chain_id"] == candidate.chain_id


def test_cold_start_health_is_degraded_but_alive_for_corrupt_protected_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "physical-backup"
    manifests = root / "protected-manifests"
    manifests.mkdir(parents=True)
    (manifests / "broken.json").write_text("{not-json")

    last, chain, error = daemon._last_durable_protected(root)
    components = _components(
        BaseCandidateState(last_protected=last, last_protected_chain=chain, restore_error=error)
    )
    restore = next(item for item in components if item["name"] == "pitr_restore_proof")
    assert (last, chain) == (None, None)
    assert restore["status"] == "degraded"
    assert restore["gate_readiness"] is False
    assert restore["protected"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    (("completed_at", "bad"), ("started_at", "2026-08-30T03:00:00")),
)
def test_cold_start_health_degrades_for_invalid_proof_semantics(
    tmp_path: Path, field: str, value: str
) -> None:
    candidate = _candidate("20260830T030000Z")
    protected = ProtectedManifest(
        schema_version=1,
        protected=True,
        chain_id=candidate.chain_id,
        candidate_sha256=candidate_sha256(candidate),
        candidate=candidate,
        base=RestoreObject("base", "base", "1", 10, "crc32c", "crc", ()),
        wal=_required_wal(candidate),
        target_lsn="0/200",
        wal_segment_size=candidate.wal_segment_size,
        proof=RestoreProof(
            "run",
            "2026-08-30T03:00:00+00:00",
            "2026-08-30T03:01:00+00:00",
            "0/200",
            "0/200",
            123,
            "live",
            "verify",
            1.0,
            1.0,
            1.0,
            10,
            "restored",
        ),
    )
    raw = json.loads(protected.to_json())
    raw["proof"][field] = value
    root = tmp_path / "physical-backup"
    manifests = root / "protected-manifests"
    manifests.mkdir(parents=True)
    (manifests / "invalid.json").write_text(json.dumps(raw))

    last, chain, error = daemon._last_durable_protected(root)

    assert (last, chain) == (None, None)
    assert error == "corrupt protected manifest(s): invalid.json"


def test_cold_start_health_degrades_for_candidate_digest_mismatch(tmp_path: Path) -> None:
    candidate = _candidate("20260830T030000Z")
    protected = ProtectedManifest(
        schema_version=1,
        protected=True,
        chain_id=candidate.chain_id,
        candidate_sha256=candidate_sha256(candidate),
        candidate=candidate,
        base=RestoreObject("base", "base", "1", 10, "crc32c", "crc", ()),
        wal=_required_wal(candidate),
        target_lsn="0/200",
        wal_segment_size=candidate.wal_segment_size,
        proof=RestoreProof(
            "run",
            "2026-08-30T03:00:00+00:00",
            "2026-08-30T03:01:00+00:00",
            "0/200",
            "0/200",
            123,
            "live",
            "verify",
            1.0,
            1.0,
            1.0,
            10,
            "restored",
        ),
    )
    raw = json.loads(protected.to_json())
    raw["candidate_sha256"] = "0" * 64
    root = tmp_path / "physical-backup"
    manifests = root / "protected-manifests"
    manifests.mkdir(parents=True)
    (manifests / "invalid.json").write_text(json.dumps(raw))

    last, chain, error = daemon._last_durable_protected(root)

    assert (last, chain) == (None, None)
    assert error == "corrupt protected manifest(s): invalid.json"


def test_cold_start_health_keeps_valid_proof_when_another_manifest_is_corrupt(
    tmp_path: Path,
) -> None:
    candidate = _candidate("20260830T030000Z")
    protected = ProtectedManifest(
        schema_version=1,
        protected=True,
        chain_id=candidate.chain_id,
        candidate_sha256=candidate_sha256(candidate),
        candidate=candidate,
        base=RestoreObject("base", "base", "1", 10, "crc32c", "crc", ()),
        wal=_required_wal(candidate),
        target_lsn="0/200",
        wal_segment_size=candidate.wal_segment_size,
        proof=RestoreProof(
            "run",
            "2026-08-30T03:00:00+00:00",
            "2026-08-30T03:01:00+00:00",
            "0/200",
            "0/200",
            123,
            "live",
            "verify",
            1.0,
            1.0,
            1.0,
            10,
            "restored",
        ),
    )
    root = tmp_path / "physical-backup"
    manifests = root / "protected-manifests"
    manifests.mkdir(parents=True)
    (manifests / f"{candidate.chain_id}.json").write_text(protected.to_json())
    (manifests / "broken.json").write_text("[]")

    last, chain, error = daemon._last_durable_protected(root)
    restore = next(
        item
        for item in _components(
            BaseCandidateState(last_protected=last, last_protected_chain=chain, restore_error=error)
        )
        if item["name"] == "pitr_restore_proof"
    )
    assert chain == candidate.chain_id
    assert restore["status"] == "degraded"
    assert restore["protected"] is True
    assert restore["gate_readiness"] is False


def test_retention_health_is_explicitly_dry_run_only(tmp_path: Path) -> None:
    result = DryRunResult(tmp_path / "plan", "digest", False, 3, 2, 30, 20)
    components = daemon._components(
        BaseCandidateState(
            retention=RetentionDryRunState(
                enabled=True, plan=result, last_attempt=time.time(), last_success=time.time()
            )
        )
    )
    retention = next(item for item in components if item["name"] == "pitr_retention_dry_run")
    assert retention["delete_enabled"] is False
    assert retention["eligible_objects"] == 2
    assert retention["eligible_bytes"] == 20


def test_retention_refresh_emits_backend_inventory_gauges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from shared.config import settings

    result = DryRunResult(tmp_path / "plan", "digest", False, 3, 2, 30, 20, 9, 90)
    config = settings.physical_backup
    monkeypatch.setattr(config, "pitr_restore_gcs_credentials_file", tmp_path / "viewer.json")
    monkeypatch.setattr(config, "pitr_retained_weekly_chains", 2)
    monkeypatch.setattr(config, "pitr_store_backend", "oss")
    calls: list[tuple[telemetry.Category, str, dict[str, object]]] = []

    class _StoreGroup:
        @staticmethod
        def retention_inventory_reader() -> object:
            return object()

    def get_store_group() -> _StoreGroup:
        return _StoreGroup()

    def write_plan(*_args: object, **_kwargs: object) -> DryRunResult:
        return result

    def record_emit(
        category: telemetry.Category,
        event_name: str,
        *,
        attributes: dict[str, object] | None = None,
    ) -> None:
        calls.append((category, event_name, attributes or {}))

    monkeypatch.setattr(retention_scheduler, "get_store_group", get_store_group)
    monkeypatch.setattr(retention_scheduler, "write_dry_run_plan", write_plan)
    monkeypatch.setattr(retention_scheduler, "ava_home", lambda: tmp_path)
    monkeypatch.setattr("shared.telemetry.emit", record_emit)

    assert retention_scheduler.refresh(config) == result
    assert calls == [
        (
            "telemetry",
            "pitr_remote_inventory",
            {"backend": "oss", "object_count": 9, "bytes": 90},
        )
    ]


def test_restore_proof_runs_once_after_the_monthly_window(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.config import settings

    monkeypatch.setattr(settings.general, "timezone", "UTC")
    before_window = datetime(2026, 9, 1, 5, 59, tzinfo=UTC)
    window = datetime(2026, 9, 1, 6, tzinfo=UTC)

    assert not daemon.restore_proof_due(before_window, last_success=None)
    assert daemon.restore_proof_due(window, last_success=None)
    assert not daemon.restore_proof_due(window, last_success=window.timestamp())
    assert daemon.restore_proof_due(
        datetime(2026, 10, 1, 6, tzinfo=UTC), last_success=window.timestamp()
    )


def test_retention_health_never_exposes_stale_or_failed_eligibility(tmp_path: Path) -> None:
    result = DryRunResult(tmp_path / "plan", "digest", False, 3, 2, 30, 20)
    state = RetentionDryRunState(
        enabled=True,
        plan=result,
        last_attempt=time.time(),
        last_success=time.time(),
        last_error="inventory unavailable",
    )
    retention = retention_health_component(state)
    assert retention["status"] == "degraded"
    assert retention["eligible_objects"] == 0
    assert retention["eligible_bytes"] == 0

    state.last_error = None
    state.last_success = time.time() - 3 * 3600
    stale = retention_health_component(state)
    assert stale["progress"] == "stale"
    assert stale["current"] is False
    assert stale["plan_digest"] is None

    state.enabled = False
    disabled = retention_health_component(state)
    assert disabled["progress"] == "disabled"
    assert disabled["current"] is False
    assert disabled["retained_objects"] == 0
    assert disabled["eligible_objects"] == 0
    assert disabled["retained_bytes"] == 0
    assert disabled["eligible_bytes"] == 0


@pytest.mark.asyncio
async def test_runner_cancellation_reaps_active_worker(
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"

    task = asyncio.create_task(
        daemon._run_worker(target=partial(_blocking_worker, started, stopped))
    )
    await _wait_for_path(started)
    child_pid = int(started.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped.read_text() == "stopped"
    await _assert_tree_gone([child_pid])


# ── QA #931 R3: domain conditions never gate readiness ────────────────────


async def _http_get_status(port: int) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    return int(status_line.split(" ")[1]), body


def _find_free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_degraded_domain_condition_keeps_healthz_200() -> None:
    """QA #931 R3 discriminator: a domain condition (cleanup pending, last
    error, stale candidate) must NOT flip /healthz to 503 — a respawn cannot
    fix it, so the watchdog would restart-flap a healthy daemon every 60s.
    The component reports degraded with gate_readiness=False; readiness
    follows process liveness only."""
    from shared.daemon_health import Liveness, start_health_server, stop_health_server

    state = BaseCandidateState(base_error="GCS credentials rejected")
    port = _find_free_port()
    liveness = Liveness(timeout_s=120)
    server = await start_health_server(
        "pitr_base_backup",
        port=port,
        liveness=liveness,
        components=lambda: _components(state),
    )
    try:
        status, body = await _http_get_status(port)
        assert status == 200, "domain condition must not gate readiness"
        payload = json.loads(body)
        assert payload["readiness"] == "ok"
        comp = next(c for c in payload["components"] if c["name"] == "pitr_base_candidate")
        assert comp["status"] == "degraded"
        assert comp["gate_readiness"] is False
        assert "GCS credentials rejected" in comp["detail"]
        # The liveness lane still gates: a dead/wedged daemon flips to 503.
        liveness._last = time.monotonic() - 1000
        status, _ = await _http_get_status(port)
        assert status == 503, "wedged daemon (stale liveness) still gates readiness"
    finally:
        await stop_health_server(server)


@pytest.mark.asyncio
async def test_forced_shutdown_reaps_noncooperative_group_and_late_fork(
    tmp_path: Path,
) -> None:
    started = tmp_path / "started"
    armed = tmp_path / "armed"
    late = tmp_path / "late"
    task = asyncio.create_task(
        daemon._run_worker(
            target=partial(_noncooperative_worker, started, armed, late),
            cooperative_timeout_s=0.1,
            group_grace_s=3,
            group_deadline_s=15,
        )
    )
    await _wait_for_path(started)
    worker_pid, child_pid = (int(value) for value in started.read_text().split())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _wait_for_path(late)
    late_pid = int(late.read_text())
    await _assert_tree_gone([worker_pid, child_pid, late_pid])
