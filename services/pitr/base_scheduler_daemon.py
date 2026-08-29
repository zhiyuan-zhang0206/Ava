"""Disabled-by-default scheduler for weekly unprotected base candidates."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import logging
import multiprocessing
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, Protocol, cast

import psutil

from services._pidfile import acquire_pidfile, remove_pidfile
from services.pitr.base_candidate import (
    StopSignal,
    create_base_candidate,
    reconcile_runtime_state,
)
from services.pitr.base_manifest import CandidateManifest
from services.pitr.base_object_store import GCSRestartableStreamingObjectStore
from services.pitr.restore_proof import (
    RestoreSpaceBudget,
    publish_candidate_proof,
    reconcile_restore_runtime,
    verify_candidate_proof,
)
from services.pitr.restore_publish_store import GCSProtectedManifestPublisher
from services.pitr.space_budget import CandidateSpaceBudget
from shared.config import settings
from shared.daemon_health import health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.db import direct_db_url
from shared.health_schema import DEGRADED, OK, component
from shared.log import init_gateway_process
from shared.paths import ava_home
from shared.pg_tools import pg_tool
from shared.platform import LockTimeoutError
from shared.process_env import restricted_process_env

_log = logging.getLogger("services.pitr.base_scheduler_daemon")

BASE_BACKUP_WEEKDAY = 6
BASE_BACKUP_HOUR = 3
BASE_BACKUP_RETRY_INTERVAL_S = 1800
BASE_BACKUP_STALE_AFTER_S = 8 * 24 * 3600
_SLEEP_CHUNK_S = 30
_EMERGENCY_FLOOR_BYTES = 4 * 1024**3
_LINUX_CHILD_ADOPTION_OPTION = 36


class _WorkerQueue(Protocol):
    def put(self, item: object) -> None: ...

    def get(self, timeout: float | None = None) -> object: ...

    def get_nowait(self) -> object: ...

    def close(self) -> None: ...

    def join_thread(self) -> None: ...


class _OwnedProcess(Protocol):
    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...


@dataclass
class BaseCandidateState:
    started_at: float = field(default_factory=time.monotonic)
    running: bool = False
    last_attempt: float | None = None
    last_success: float | None = None
    last_error: str | None = None
    deferred_for_logical_backup: bool = False
    cleanup_pending: bool = False
    restore_running: bool = False
    last_protected: float | None = None


@dataclass(frozen=True)
class _RestoreWorkerInput:
    candidate_json: str
    root: Path
    ack_dir: Path
    key_path: Path
    project: str
    bucket: str
    viewer_credentials: Path
    budget: RestoreSpaceBudget
    live_db_url: str
    pg_ctl: Path
    pg_verifybackup: Path


def _candidate_manifests(root: Path) -> list[CandidateManifest]:
    manifests: list[CandidateManifest] = []
    if not root.exists():
        return manifests
    for path in root.glob("*.candidate.json"):
        manifests.append(CandidateManifest.from_json(path.read_text()))
    return manifests


def is_due(now: datetime, root: Path) -> bool:
    """Use durable manifests so a daemon restart cannot repeat this week."""

    candidates = _candidate_manifests(root)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    due_at = week_start + timedelta(days=BASE_BACKUP_WEEKDAY, hours=BASE_BACKUP_HOUR)
    if due_at > now:
        due_at -= timedelta(days=7)
    if candidates:
        newest = max(_candidate_time(item) for item in candidates)
        return newest < due_at
    return True


def _candidate_time(candidate: CandidateManifest) -> datetime:
    return datetime.strptime(candidate.chain_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def _last_durable_success(root: Path) -> float | None:
    candidates = _candidate_manifests(root)
    if not candidates:
        return None
    return max(_candidate_time(item).timestamp() for item in candidates)


def _components(state: BaseCandidateState) -> list[dict[str, object]]:
    if state.cleanup_pending:
        status, detail = DEGRADED, state.last_error or "completed candidate cleanup is pending"
        progress = "cleanup"
    elif state.running:
        status, detail = OK, None
        progress = "running"
    elif state.last_error:
        status, detail = DEGRADED, state.last_error
        progress = "idle"
    elif state.last_success and time.time() - state.last_success > BASE_BACKUP_STALE_AFTER_S:
        status, detail = DEGRADED, "last base candidate is older than eight days"
        progress = "idle"
    else:
        status, detail, progress = OK, None, "idle"
    # The candidate's state is a domain condition (cleanup pending, GCS
    # credentials, replication contract, staleness) that a restart cannot
    # fix; gating readiness would make the watchdog respawn a healthy daemon
    # every 60s onto the same condition (QA #931 R3, #927 arbitration A).
    # Readiness follows process liveness only — /healthz 503 means the
    # daemon is dead.
    record = component(
        "pitr_base_candidate",
        status,
        last_success=state.last_success,
        progress=progress,
        detail=detail,
        now=time.time() if state.last_success else None,
        gate_readiness=False,
    )
    record["protected"] = False
    record["deferred_for_logical_backup"] = state.deferred_for_logical_backup
    record["cleanup_pending"] = state.cleanup_pending
    restore_status = OK if state.last_protected or not state.last_error else DEGRADED
    restore = component(
        "pitr_restore_proof",
        restore_status,
        last_success=state.last_protected,
        progress="running" if state.restore_running else "idle",
        detail=state.last_error if restore_status == DEGRADED else None,
        now=time.time() if state.last_protected else None,
        gate_readiness=False,
    )
    restore["protected"] = state.last_protected is not None
    return [record, restore]


def _tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _build_candidate(stop: StopSignal) -> CandidateManifest:
    config = settings.physical_backup
    if not config.pitr_base_backup_enabled:
        raise RuntimeError("base candidate scheduler cannot run while its flag is off")
    key_path = config.pitr_backup_key_file
    credentials = config.pitr_gcs_credentials_file
    if key_path is None or credentials is None:
        raise RuntimeError("validated PITR secrets are missing")
    root = ava_home() / "physical-backup"
    pgdata_bytes = _tree_bytes(ava_home() / "pg")
    logical_peak = max(
        (item.stat().st_size for item in (ava_home() / "backups" / "db").glob("*.enc")),
        default=0,
    )
    budget = CandidateSpaceBudget(
        compressed_staging_estimate=pgdata_bytes,
        spool_and_pg_wal_reserve=config.pitr_spool_hard_bytes,
        logical_backup_peak_reserve=logical_peak,
        emergency_floor=_EMERGENCY_FLOOR_BYTES,
    )
    return create_base_candidate(
        root=root,
        prefix=config.pitr_gcs_prefix,
        key=key_path.read_bytes(),
        key_id=config.pitr_backup_key_id,
        store=GCSRestartableStreamingObjectStore(
            project=config.pitr_gcs_project,
            bucket=config.pitr_gcs_bucket,
            credentials_file=str(credentials),
        ),
        budget=budget,
        replication_db_url=config.pitr_replication_db_url,
        stop=stop,
    )


def _pending_restore_candidate(root: Path) -> CandidateManifest | None:
    protected = root / "protected-manifests"
    for candidate in _candidate_manifests(root / "base-manifests"):
        if not (protected / f"{candidate.chain_id}.json").is_file():
            return candidate
    return None


def _restore_worker_input() -> _RestoreWorkerInput:
    config = settings.physical_backup
    if not config.pitr_restore_proof_enabled:
        raise RuntimeError("restore proof cannot run while its flag is off")
    key_path = config.pitr_backup_key_file
    read_credentials = config.pitr_restore_gcs_credentials_file
    if key_path is None or read_credentials is None:
        raise RuntimeError("validated viewer-only restore proof secrets are missing")
    root = ava_home() / "physical-backup"
    candidate = _pending_restore_candidate(root)
    if candidate is None:
        raise RuntimeError("restore proof has no unprotected candidate")
    logical_peak = max(
        (item.stat().st_size for item in (ava_home() / "backups" / "db").glob("*.enc")),
        default=0,
    )
    return _RestoreWorkerInput(
        candidate.to_json(),
        root,
        root / "ack",
        key_path,
        config.pitr_gcs_project,
        config.pitr_gcs_bucket,
        read_credentials,
        RestoreSpaceBudget(
            config.pitr_spool_hard_bytes,
            logical_peak,
            _EMERGENCY_FLOOR_BYTES,
        ),
        direct_db_url(),
        pg_tool("pg_ctl"),
        pg_tool("pg_verifybackup"),
    )


def _publish_restore_proof(candidate: CandidateManifest, outcome: dict[str, str]) -> None:
    """Publish durable proof only from the controller that owns uploader authority."""

    config = settings.physical_backup
    credentials = config.pitr_gcs_credentials_file
    if credentials is None:
        raise RuntimeError("validated PITR publisher credential is missing")
    root = ava_home() / "physical-backup"
    manifest_path = root / "base-manifests" / f"{candidate.chain_id}.candidate.json"
    authoritative = CandidateManifest.from_json(manifest_path.read_text())
    candidate_digest = hashlib.sha256(authoritative.to_json().encode()).hexdigest()
    pending = root / "protected-pending" / f"{authoritative.chain_id}.json"
    if (
        authoritative != candidate
        or outcome.get("chain_id") != authoritative.chain_id
        or outcome.get("candidate_sha256") != candidate_digest
        or outcome.get("pending_sha256") != hashlib.sha256(pending.read_bytes()).hexdigest()
    ):
        raise RuntimeError("restricted restore outcome differs from authoritative evidence")
    verified = verify_candidate_proof(
        candidate=authoritative,
        root=root,
        ack_dir=root / "ack",
    )
    publisher = GCSProtectedManifestPublisher(
        project=config.pitr_gcs_project,
        bucket=config.pitr_gcs_bucket,
        credentials_file=credentials,
    )
    publish_candidate_proof(
        candidate=authoritative,
        root=root,
        prefix=config.pitr_gcs_prefix,
        verified=verified,
        publisher=publisher,
    )


async def _sleep(seconds: float) -> None:
    remaining = seconds
    while remaining > 0:
        chunk = min(_SLEEP_CHUNK_S, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk


def _worker_entry(stop: StopSignal, output: _WorkerQueue) -> None:
    try:
        output.put((True, _build_candidate(stop).to_json()))
    except BaseException as exc:
        output.put((False, f"{type(exc).__name__}: {exc}"))


def _restore_request(inputs: _RestoreWorkerInput) -> dict[str, object]:
    return {
        "candidate_json": inputs.candidate_json,
        "root": str(inputs.root),
        "ack_dir": str(inputs.ack_dir),
        "key_path": str(inputs.key_path),
        "project": inputs.project,
        "bucket": inputs.bucket,
        "viewer_credentials": str(inputs.viewer_credentials),
        "budget": {
            "spool_and_pg_wal_reserve": inputs.budget.spool_and_pg_wal_reserve,
            "logical_backup_peak": inputs.budget.logical_backup_peak,
            "emergency_floor": inputs.budget.emergency_floor,
        },
        "live_db_url": inputs.live_db_url,
        "pg_ctl": str(inputs.pg_ctl),
        "pg_verifybackup": str(inputs.pg_verifybackup),
    }


async def _run_restore_worker(inputs: _RestoreWorkerInput) -> dict[str, str]:
    control_root = inputs.root / "restore-control"
    control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = Path(tempfile.mkdtemp(prefix=".proof-", dir=control_root))
    request = work / "request.json"
    result = work / "result.json"
    request.write_text(json.dumps(_restore_request(inputs), sort_keys=True, separators=(",", ":")))
    request.chmod(0o600)
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "services.pitr.restore_worker", str(request), str(result)],
        cwd=Path(__file__).resolve().parents[2],
        env=restricted_process_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
        text=True,
    )
    leader_created_at = psutil.Process(process.pid).create_time()
    try:
        while process.poll() is None:
            await asyncio.sleep(0.25)
        if _group_members(process.pid):
            _reap_restore_subprocess_group(process, leader_created_at)
            _reject_restore_descendants()
        stderr = process.stderr.read() if process.stderr is not None else ""
        return _restore_worker_result(process.returncode, stderr, result)
    except BaseException:
        if process.poll() is None or _group_members(process.pid):
            _reap_restore_subprocess_group(process, leader_created_at)
        raise
    finally:
        if process.stderr is not None:
            process.stderr.close()
        shutil.rmtree(work, ignore_errors=True)


def _reap_restore_subprocess_group(
    process: subprocess.Popen[str], leader_created_at: float
) -> None:
    if process.pid == os.getpgrp():
        raise RuntimeError("refusing to signal the controller process group")
    with suppress(psutil.NoSuchProcess):
        leader = psutil.Process(process.pid)
        if abs(leader.create_time() - leader_created_at) >= 0.01:
            raise RuntimeError("restricted restore worker PID identity changed")
    deadline = time.monotonic() + 20
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    grace = min(deadline, time.monotonic() + 5)
    while _group_members(process.pid) and time.monotonic() < grace:
        time.sleep(0.1)
    while _group_members(process.pid) and time.monotonic() < deadline:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        time.sleep(0.1)
    process.wait(timeout=max(0.1, deadline - time.monotonic()))
    if _group_members(process.pid):
        raise RuntimeError("restricted restore worker process group could not be emptied")


def _reject_restore_descendants() -> NoReturn:
    raise RuntimeError("restricted restore worker left live descendants")


def _restore_worker_result(returncode: int, stderr: str, result: Path) -> dict[str, str]:
    if returncode != 0:
        raise RuntimeError(f"restricted restore worker exited {returncode}: {stderr}")
    loaded: object = json.loads(result.read_text())
    if not isinstance(loaded, dict):
        raise TypeError("restricted restore worker result must be an object")
    raw = cast(dict[str, object], loaded)
    if set(raw) != {
        "chain_id",
        "candidate_sha256",
        "pending_sha256",
    }:
        raise RuntimeError("restricted restore worker returned an invalid result")
    return {key: str(value) for key, value in raw.items()}


def _worker_bootstrap(
    target: Callable[[StopSignal, _WorkerQueue], None],
    stop: StopSignal,
    output: _WorkerQueue,
) -> None:
    os.setsid()
    process = psutil.Process()
    output.put(
        (
            "ready",
            str(process.pid),
            str(os.getpgrp()),
            repr(process.create_time()),
        )
    )
    target(stop, output)


def _group_members(pgid: int) -> list[psutil.Process]:
    members: list[psutil.Process] = []
    for process in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(process.pid) == pgid:
                members.append(process)
        except (ProcessLookupError, PermissionError, psutil.NoSuchProcess):
            continue
    return members


def _enable_child_subreaper() -> None:
    if sys.platform != "linux":
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_LINUX_CHILD_ADOPTION_OPTION, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, "could not own orphaned base candidate descendants")


def _reap_exited_group_children(process: _OwnedProcess, pgid: int) -> None:
    process.join(timeout=0)
    while True:
        try:
            pid, _status = os.waitpid(-pgid, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _reap_job_group(
    process: _OwnedProcess,
    *,
    worker_pid: int,
    pgid: int,
    leader_created_at: float,
    grace_s: float = 5,
    deadline_s: float = 20,
) -> None:
    if pgid != worker_pid or pgid == os.getpgrp():
        raise RuntimeError("refusing to signal an unowned base candidate process group")
    leader: psutil.Process | None = None
    with suppress(psutil.NoSuchProcess):
        leader = psutil.Process(worker_pid)
    if leader is not None and abs(leader.create_time() - leader_created_at) >= 0.01:
        raise RuntimeError("base candidate worker PID identity changed")
    deadline = time.monotonic() + deadline_s
    with suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    grace_end = min(deadline, time.monotonic() + grace_s)
    _reap_exited_group_children(process, pgid)
    while _group_members(pgid) and time.monotonic() < grace_end:
        time.sleep(0.1)
        _reap_exited_group_children(process, pgid)
    while _group_members(pgid) and time.monotonic() < deadline:
        with suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGKILL)
        time.sleep(0.1)
        _reap_exited_group_children(process, pgid)
    if _group_members(pgid):
        raise RuntimeError("base candidate process group could not be emptied")
    process.join(timeout=max(0, deadline - time.monotonic()))
    if process.is_alive():
        raise RuntimeError("base candidate worker leader could not be reaped")


def _worker_result(*, succeeded: bool, value: str) -> CandidateManifest:
    if not succeeded:
        raise RuntimeError(value)
    return CandidateManifest.from_json(value)


def _validate_ready_message(
    message: tuple[str, str, str, str], *, expected_pid: int
) -> tuple[int, float]:
    kind, raw_pid, raw_pgid, raw_created_at = message
    if kind != "ready" or int(raw_pid) != expected_pid or int(raw_pgid) != expected_pid:
        raise RuntimeError("base candidate worker reported invalid process ownership")
    return int(raw_pgid), float(raw_created_at)


def _raise_live_descendants() -> NoReturn:
    raise RuntimeError("base candidate worker left live descendants")


def _backup_key() -> tuple[bytes, str]:
    config = settings.physical_backup
    key_path = config.pitr_backup_key_file
    if key_path is None:
        raise RuntimeError("validated PITR backup key is missing")
    return key_path.read_bytes(), config.pitr_backup_key_id


async def _run_worker(
    *,
    target: Callable[[StopSignal, _WorkerQueue], None] = _worker_entry,
    cooperative_timeout_s: float = 30,
    group_grace_s: float = 5,
    group_deadline_s: float = 20,
) -> CandidateManifest:
    _enable_child_subreaper()
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    output = cast(_WorkerQueue, context.Queue(maxsize=2))
    process = context.Process(target=_worker_bootstrap, args=(target, stop, output), daemon=False)
    process.start()
    worker_pid = process.pid
    if worker_pid is None:
        process.kill()
        process.join(timeout=5)
        raise RuntimeError("base candidate worker started without a PID")
    pgid: int | None = None
    leader_created_at: float | None = None
    try:
        ready_deadline = time.monotonic() + 10
        while pgid is None:
            try:
                message = cast(tuple[str, str, str, str], output.get_nowait())
            except queue.Empty:
                if not process.is_alive() or time.monotonic() >= ready_deadline:
                    raise RuntimeError(
                        "base candidate worker failed before ownership handshake"
                    ) from None
                await asyncio.sleep(0.05)
                continue
            pgid, leader_created_at = _validate_ready_message(message, expected_pid=worker_pid)
        while process.is_alive():
            await asyncio.sleep(0.25)
        process.join()
        if _group_members(pgid):
            _reap_job_group(
                process,
                worker_pid=worker_pid,
                pgid=pgid,
                leader_created_at=cast(float, leader_created_at),
                grace_s=group_grace_s,
                deadline_s=group_deadline_s,
            )
            _raise_live_descendants()
        try:
            succeeded, value = cast(tuple[bool, str], output.get(timeout=5))
        except queue.Empty as exc:
            raise RuntimeError("base candidate worker exited without a result") from exc
        return _worker_result(succeeded=succeeded, value=value)
    except BaseException:
        stop.set()
        process.join(timeout=cooperative_timeout_s)
        if pgid is not None and leader_created_at is not None and _group_members(pgid):
            _reap_job_group(
                process,
                worker_pid=worker_pid,
                pgid=pgid,
                leader_created_at=leader_created_at,
                grace_s=group_grace_s,
                deadline_s=group_deadline_s,
            )
        elif process.is_alive():
            process.kill()
            process.join(timeout=5)
        if process.is_alive():
            raise RuntimeError("base candidate worker could not be reaped") from None
        raise
    finally:
        output.close()
        output.join_thread()


async def _loop(state: BaseCandidateState) -> None:  # noqa: PLR0915
    root = ava_home() / "physical-backup" / "base-manifests"
    while True:
        state.cleanup_pending = True
        try:
            reconcile_restore_runtime(ava_home() / "physical-backup")
            key, key_id = _backup_key()
            reconcile_runtime_state(
                ava_home() / "physical-backup",
                key=key,
                key_id=key_id,
            )
        except Exception as exc:
            state.last_error = str(exc)
            _log.exception("base candidate reconciliation failed")
            await _sleep(BASE_BACKUP_RETRY_INTERVAL_S)
            continue
        else:
            state.cleanup_pending = False
        config = settings.physical_backup
        if config.pitr_restore_proof_enabled and _pending_restore_candidate(
            ava_home() / "physical-backup"
        ):
            state.restore_running = True
            try:
                inputs = _restore_worker_input()
                outcome = await _run_restore_worker(inputs)
                candidate = CandidateManifest.from_json(inputs.candidate_json)
                _publish_restore_proof(candidate, outcome)
                state.last_protected = time.time()
                state.last_error = None
            except Exception as exc:
                state.last_error = str(exc)
                _log.exception("restore proof failed; candidate remains unprotected")
            finally:
                state.restore_running = False
            await _sleep(BASE_BACKUP_RETRY_INTERVAL_S)
            continue
        now = datetime.now(UTC)
        if not is_due(now, root):
            await _sleep(3600)
            continue
        state.last_attempt = now.timestamp()
        state.running = True
        state.deferred_for_logical_backup = False
        try:
            await _run_worker()
            state.last_success = time.time()
            state.last_error = None
        except LockTimeoutError:
            state.deferred_for_logical_backup = True
            _log.info("base candidate deferred while logical backup owns backup lock")
        except Exception as exc:
            state.last_error = str(exc)
            _log.exception("base candidate failed; retrying on bounded cadence")
        finally:
            state.running = False
        await _sleep(BASE_BACKUP_RETRY_INTERVAL_S)


async def run() -> None:
    pidfile = settings.services.pitr_base_backup_pidfile
    if not acquire_pidfile(pidfile, "services.pitr.base_scheduler_daemon"):
        return
    root = ava_home() / "physical-backup" / "base-manifests"
    physical_root = ava_home() / "physical-backup"
    cleanup_pending = (
        any((physical_root / "restore").glob(".*.partial"))
        or any((physical_root / "base-candidates").glob(".*.partial"))
        or any(
            (
                physical_root
                / "base-manifests"
                / f"{path.name.removesuffix('.ready')}.candidate.json"
            ).is_file()
            for path in (physical_root / "base-candidates").glob("*.ready")
        )
    )
    state = BaseCandidateState(
        last_success=_last_durable_success(root), cleanup_pending=cleanup_pending
    )
    if state.last_success is None and is_due(datetime.now(UTC), root):
        state.last_error = "no durable base candidate exists for the most recent weekly window"
    health = await start_health_server("pitr_base_backup", components=lambda: _components(state))
    _log.info("PITR base candidate healthz listening on :%s", health_port("pitr_base_backup"))
    try:
        await _loop(state)
    finally:
        await stop_health_server(health)
        remove_pidfile(pidfile)


def main() -> None:
    init_gateway_process(name="pitr-base-candidate")
    install_graceful_shutdown("pitr-base-candidate")
    asyncio.run(run())


if __name__ == "__main__":
    main()
