"""Disabled-by-default scheduler for weekly unprotected base candidates."""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import queue
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import psutil

from services._pidfile import acquire_pidfile, remove_pidfile
from services.pitr.activation_state import load_record as load_activation_record
from services.pitr.activation_state import lock_path as activation_lock_path
from services.pitr.base_candidate import (
    StopSignal,
    create_base_candidate,
    reconcile_runtime_state,
)
from services.pitr.base_manifest import CandidateManifest
from services.pitr.base_object_store import GCSRestartableStreamingObjectStore
from services.pitr.base_operation_runtime import (
    RestoreWorkerInput as _RestoreWorkerInput,
)
from services.pitr.base_operation_runtime import (
    input_for as _restore_worker_input_for,
)
from services.pitr.base_operation_runtime import (
    publish as _publish_restore_proof,
)
from services.pitr.base_operation_runtime import (
    reap_restore_group as _reap_restore_subprocess_group,
)
from services.pitr.base_operation_runtime import (
    restore_result as _restore_worker_result,
)
from services.pitr.base_operation_runtime import (
    run_restore_input as _run_restore_worker,
)
from services.pitr.base_operation_runtime import (
    verify_then_construct_publisher as _verify_then_construct_publisher,
)
from services.pitr.base_scheduler_health import components as _components
from services.pitr.restore_manifest import ProtectedManifest
from services.pitr.restore_proof import reconcile_restore_runtime
from services.pitr.retention_scheduler import (
    RetentionDryRunState,
)
from services.pitr.retention_scheduler import (
    refresh as refresh_retention_plan,
)
from services.pitr.space_budget import CandidateSpaceBudget
from services.pitr.worker_process import WorkerQueue as _WorkerQueue
from services.pitr.worker_process import enable_child_subreaper as _enable_child_subreaper
from services.pitr.worker_process import group_members as _group_members
from services.pitr.worker_process import raise_live_descendants as _raise_live_descendants
from services.pitr.worker_process import reap_job_group as _reap_job_group
from services.pitr.worker_process import validate_ready_message as _validate_ready_message
from services.pitr.worker_process import worker_bootstrap as _worker_bootstrap
from shared.config import settings
from shared.daemon_health import health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.log import init_gateway_process
from shared.paths import ava_home
from shared.platform import LockTimeoutError, file_lock
from shared.private_storage import ensure_private_dir

_log = logging.getLogger("services.pitr.base_scheduler_daemon")

# Compatibility surface for tests and operators that imported the daemon's
# former private restore-controller names before the implementation moved.
__all__ = (
    "_RestoreWorkerInput",
    "_group_members",
    "_publish_restore_proof",
    "_reap_restore_subprocess_group",
    "_restore_worker_input_for",
    "_restore_worker_result",
    "_run_restore_worker",
    "_verify_then_construct_publisher",
)

BASE_BACKUP_WEEKDAY = 6
BASE_BACKUP_HOUR = 3
BASE_BACKUP_RETRY_INTERVAL_S = 1800

BASE_BACKUP_STALE_AFTER_S = 8 * 24 * 3600
_SLEEP_CHUNK_S = 30
_EMERGENCY_FLOOR_BYTES = 4 * 1024**3
@dataclass
class BaseCandidateState:
    started_at: float = field(default_factory=time.monotonic)
    running: bool = False
    last_attempt: float | None = None
    last_success: float | None = None
    base_error: str | None = None
    deferred_for_logical_backup: bool = False
    cleanup_pending: bool = False
    restore_running: bool = False
    last_protected: float | None = None
    last_protected_chain: str | None = None
    restore_error: str | None = None
    retention: RetentionDryRunState = field(default_factory=RetentionDryRunState)


def _candidate_manifests(root: Path) -> list[CandidateManifest]:
    manifests: list[CandidateManifest] = []
    if not root.exists():
        return manifests
    for path in root.glob("*.candidate.json"):
        manifests.append(CandidateManifest.from_json(path.read_text()))
    return manifests


def is_due(now: datetime, root: Path) -> bool:
    """Use durable manifests so a daemon restart cannot repeat this week."""

    candidates = [
        item for item in _candidate_manifests(root) if not item.chain_id.startswith("activation-")
    ]
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
    stamp = (
        candidate.chain_id.split("-", 2)[1]
        if candidate.chain_id.startswith("activation-")
        else candidate.chain_id
    )
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def _last_durable_success(root: Path) -> float | None:
    candidates = [
        item for item in _candidate_manifests(root) if not item.chain_id.startswith("activation-")
    ]
    if not candidates:
        return None
    return max(_candidate_time(item).timestamp() for item in candidates)


def _last_durable_protected(
    root: Path,
) -> tuple[float | None, str | None, str | None]:
    manifests: list[ProtectedManifest] = []
    corrupt: list[str] = []
    for path in sorted((root / "protected-manifests").glob("*.json")):
        try:
            manifests.append(ProtectedManifest.from_json(path.read_text()))
        except (OSError, TypeError, ValueError):
            corrupt.append(path.name)
    error = f"corrupt protected manifest(s): {', '.join(corrupt)}" if corrupt else None
    if not manifests:
        return None, None, error
    latest = max(manifests, key=lambda item: datetime.fromisoformat(item.proof.completed_at))
    return datetime.fromisoformat(latest.proof.completed_at).timestamp(), latest.chain_id, error


def _build_candidate(stop: StopSignal) -> CandidateManifest:
    config = settings.physical_backup
    if not config.pitr_base_backup_enabled:
        raise RuntimeError("base candidate scheduler cannot run while its flag is off")
    key_path = config.pitr_backup_key_file
    credentials = config.pitr_gcs_credentials_file
    if key_path is None or credentials is None:
        raise RuntimeError("validated PITR secrets are missing")
    root = ava_home() / "physical-backup"
    pgdata_bytes = sum(
        item.stat().st_size for item in (ava_home() / "pg").rglob("*") if item.is_file()
    )
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
        if candidate.chain_id.startswith("activation-"):
            continue
        if not (protected / f"{candidate.chain_id}.json").is_file():
            return candidate
    return None


@contextmanager
def _claim_scheduler_ownership() -> Iterator[None]:
    """Serialize selection and execution with activation, then recheck state."""

    ensure_private_dir(activation_lock_path(ava_home()).parent)
    with file_lock(activation_lock_path(ava_home()), timeout_s=0):
        active = load_activation_record(ava_home())
        if active is not None and active.phase not in {"protected", "rolled_back"}:
            raise RuntimeError("activation owns base/restore selection")
        yield


def _restore_worker_input() -> _RestoreWorkerInput:
    root = ava_home() / "physical-backup"
    candidate = _pending_restore_candidate(root)
    if candidate is None:
        raise RuntimeError("restore proof has no unprotected candidate")
    return _restore_worker_input_for(candidate)


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


def _worker_result(*, succeeded: bool, value: str) -> CandidateManifest:
    if not succeeded:
        raise RuntimeError(value)
    return CandidateManifest.from_json(value)


def _backup_key() -> tuple[bytes, str]:
    config = settings.physical_backup
    key_path = config.pitr_backup_key_file
    if key_path is None:
        raise RuntimeError("validated PITR backup key is missing")
    return key_path.read_bytes(), config.pitr_backup_key_id


def _reconcile_owned_runtime() -> None:
    """Reconcile only after the activation lock and its post-lock state check."""

    with _claim_scheduler_ownership():
        key, key_id = _backup_key()
        reconcile_restore_runtime(ava_home() / "physical-backup")
        reconcile_runtime_state(
            ava_home() / "physical-backup",
            key=key,
            key_id=key_id,
        )


def _record_protected(state: BaseCandidateState, candidate: CandidateManifest) -> None:
    state.last_protected = time.time()
    state.last_protected_chain = candidate.chain_id
    state.restore_error = None


async def _run_worker(  # noqa: PLR0915
    *,
    target: Callable[[StopSignal, _WorkerQueue], None] = _worker_entry,
    cooperative_timeout_s: float = 30,
    group_grace_s: float = 5,
    group_deadline_s: float = 20,
) -> CandidateManifest:
    _enable_child_subreaper()
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    adopted = context.Event()
    output = cast(_WorkerQueue, context.Queue(maxsize=2))
    process = context.Process(
        target=_worker_bootstrap, args=(target, stop, output, adopted), daemon=False
    )
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
            adopted.set()  # release the ownership gate; worker may fork now
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
            _reconcile_owned_runtime()
        except Exception as exc:
            state.base_error = str(exc)
            _log.exception("base candidate reconciliation failed")
            await _sleep(BASE_BACKUP_RETRY_INTERVAL_S)
            continue
        else:
            state.cleanup_pending = False
        activation = load_activation_record(ava_home())
        if activation is not None and activation.phase not in {"protected", "rolled_back"}:
            # The activation CLI owns its exact candidate/restore chain. The
            # weekly loop must not select or publish unrelated work meanwhile.
            await _sleep(BASE_BACKUP_RETRY_INTERVAL_S)
            continue
        config = settings.physical_backup
        state.retention.enabled = config.pitr_retention_planner_enabled
        if config.pitr_retention_planner_enabled:
            state.retention.last_attempt = time.time()
            try:
                state.retention.plan = refresh_retention_plan(config)
                state.retention.last_success = time.time()
                state.retention.last_error = None
            except Exception as exc:
                state.retention.plan = None
                state.retention.last_error = str(exc)
                _log.exception("PITR retention dry-run planning failed")
                await _sleep(BASE_BACKUP_RETRY_INTERVAL_S)
                continue
        if config.pitr_restore_proof_enabled and _pending_restore_candidate(
            ava_home() / "physical-backup"
        ):
            state.restore_running = True
            try:
                with _claim_scheduler_ownership():
                    inputs = _restore_worker_input()
                    outcome = await _run_restore_worker(
                        inputs
                    )  # async-blocking-ok: ownership lock spans child proof
                    candidate = CandidateManifest.from_json(inputs.candidate_json)
                    _publish_restore_proof(candidate, outcome)
                _record_protected(state, candidate)
            except Exception as exc:
                state.restore_error = str(exc)
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
            with _claim_scheduler_ownership():
                await _run_worker()  # async-blocking-ok: ownership lock spans candidate child
            state.last_success = time.time()
            state.base_error = None
        except LockTimeoutError:
            state.deferred_for_logical_backup = True
            _log.info("base candidate deferred while logical backup owns backup lock")
        except Exception as exc:
            state.base_error = str(exc)
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
    last_protected, last_protected_chain, protected_scan_error = _last_durable_protected(
        physical_root
    )
    state = BaseCandidateState(
        last_success=_last_durable_success(root),
        cleanup_pending=cleanup_pending,
        last_protected=last_protected,
        last_protected_chain=last_protected_chain,
        restore_error=protected_scan_error,
    )
    if state.last_success is None and is_due(datetime.now(UTC), root):
        state.base_error = "no durable base candidate exists for the most recent weekly window"
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
