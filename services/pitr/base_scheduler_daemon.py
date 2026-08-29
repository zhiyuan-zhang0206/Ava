"""Disabled-by-default scheduler for weekly unprotected base candidates."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import multiprocessing
import os
import queue
import signal
import sys
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
from services.pitr.space_budget import CandidateSpaceBudget
from shared.config import settings
from shared.daemon_health import health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import install_graceful_shutdown
from shared.health_schema import DEGRADED, OK, component
from shared.log import init_gateway_process
from shared.paths import ava_home
from shared.platform import LockTimeoutError

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
    return [record]


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


async def _loop(state: BaseCandidateState) -> None:
    root = ava_home() / "physical-backup" / "base-manifests"
    while True:
        state.cleanup_pending = True
        try:
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
    cleanup_pending = any((physical_root / "base-candidates").glob(".*.partial")) or any(
        (
            physical_root / "base-manifests" / f"{path.name.removesuffix('.ready')}.candidate.json"
        ).is_file()
        for path in (physical_root / "base-candidates").glob("*.ready")
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
