"""Disabled-by-default scheduler for weekly unprotected base candidates."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from services._pidfile import acquire_pidfile, remove_pidfile
from services.pitr.activation_state import load_record as load_activation_record
from services.pitr.activation_state import lock_path as activation_lock_path
from services.pitr.base_candidate import reconcile_committed_cleanup
from services.pitr.base_manifest import CandidateManifest
from services.pitr.base_operation_runtime import (
    RestoreWorkerInput,
    input_for,
    publish,
    run_restore_input,
)
from services.pitr.base_scheduler_health import components as _components
from services.pitr.base_worker import run_candidate
from services.pitr.operation_custody import OperationDeferred
from services.pitr.restore_manifest import ProtectedManifest
from services.pitr.restore_proof import reconcile_restore_pending
from services.pitr.retention_scheduler import (
    RetentionDryRunState,
)
from services.pitr.retention_scheduler import (
    delete_tick as retention_delete_tick,
)
from services.pitr.retention_scheduler import (
    refresh as refresh_retention_plan,
)
from shared import telemetry
from shared.config import settings
from shared.daemon_health import health_port, start_health_server, stop_health_server
from shared.daemon_shutdown import cancel_and_drain, install_graceful_shutdown
from shared.daemon_shutdown import hard_exit as _hard_exit
from shared.log import init_gateway_process
from shared.paths import ava_home
from shared.platform import LockTimeoutError, file_lock
from shared.private_storage import ensure_private_dir

_log = logging.getLogger("services.pitr.base_scheduler_daemon")

BASE_BACKUP_WEEKDAY = 6
BASE_BACKUP_HOUR = 3
BASE_BACKUP_RETRY_INTERVAL_S = 1800

BASE_BACKUP_STALE_AFTER_S = 8 * 24 * 3600
_SLEEP_CHUNK_S = 30
RESTORE_PROOF_MONTHLY_DAY = 1
RESTORE_PROOF_HOUR = 6


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


def restore_proof_due(now: datetime, *, last_success: float | None) -> bool:
    """Whether the current calendar month's PITR proof window lacks success."""
    local_now = now.astimezone(ZoneInfo(settings.general.timezone))
    scheduled = local_now.replace(
        day=RESTORE_PROOF_MONTHLY_DAY,
        hour=RESTORE_PROOF_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    if local_now < scheduled:
        return False
    return last_success is None or last_success < scheduled.timestamp()


def _pending_restore_candidate(root: Path) -> CandidateManifest | None:
    protected = root / "protected-manifests"
    for candidate in _candidate_manifests(root / "base-manifests"):
        if candidate.chain_id.startswith("activation-"):
            continue
        if not (protected / f"{candidate.chain_id}.json").is_file():
            return candidate
    return None


def _claim_scheduler_ownership() -> ExitStack:
    """Serialize selection and execution with activation, then recheck state."""

    stack = ExitStack()
    ensure_private_dir(activation_lock_path(ava_home()).parent)
    stack.enter_context(file_lock(activation_lock_path(ava_home()), timeout_s=0))
    try:
        _require_scheduler_idle()
    except BaseException:
        stack.close()
        raise
    return stack


def _require_scheduler_idle() -> None:
    active = load_activation_record(ava_home())
    if active is not None and active.phase not in {"protected", "rolled_back"}:
        raise RuntimeError("activation owns base/restore selection")


def _restore_worker_input() -> RestoreWorkerInput:
    root = ava_home() / "physical-backup"
    candidate = _pending_restore_candidate(root)
    if candidate is None:
        raise RuntimeError("restore proof has no unprotected candidate")
    return input_for(candidate)


async def _sleep(seconds: float) -> None:
    remaining = seconds
    while remaining > 0:
        chunk = min(_SLEEP_CHUNK_S, remaining)
        await asyncio.sleep(chunk)
        remaining -= chunk


def _backup_key() -> tuple[bytes, str]:
    config = settings.physical_backup
    key_path = config.pitr_backup_key_file
    if key_path is None:
        raise RuntimeError("validated PITR backup key is missing")
    return key_path.read_bytes(), config.pitr_backup_key_id


def _reconcile_owned_runtime() -> None:
    """Reconcile only after the activation lock and its post-lock state check.

    Only committed work is cleaned here. An operation's unsettled staging
    belongs to its own kind (quarantined, or a blocked control root) and never
    stalls retention planning or the other kind's schedule.
    """

    with _claim_scheduler_ownership():
        key, key_id = _backup_key()
        reconcile_restore_pending(ava_home() / "physical-backup")
        reconcile_committed_cleanup(ava_home() / "physical-backup", key=key, key_id=key_id)


def _record_protected(state: BaseCandidateState, candidate: CandidateManifest) -> None:
    state.last_protected = time.time()
    state.last_protected_chain = candidate.chain_id
    state.restore_error = None


async def _loop(state: BaseCandidateState) -> None:
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
                # The inventory read is synchronous and network-bound (66-90s
                # measured), and the health server shares this loop, so run it
                # off-loop.
                state.retention.plan = await asyncio.to_thread(refresh_retention_plan, config)
                state.retention.last_success = time.time()
                state.retention.last_error = None
            except Exception as exc:
                state.retention.plan = None
                state.retention.last_error = str(exc)
                _log.exception("PITR retention dry-run planning failed")
                await _sleep(BASE_BACKUP_RETRY_INTERVAL_S)
                continue
        # One arm-gated deletion pass per tick (a no-op while disabled). The
        # executor is synchronous and paces itself across up to minutes, and
        # the health server shares this loop, so run it off-loop.
        await asyncio.to_thread(retention_delete_tick, state.retention, config)
        if (
            config.pitr_restore_proof_enabled
            and restore_proof_due(datetime.now(UTC), last_success=state.last_protected)
            and _pending_restore_candidate(ava_home() / "physical-backup")
        ):
            await _restore_job(state)
            await _sleep(BASE_BACKUP_RETRY_INTERVAL_S)
            continue
        now = datetime.now(UTC)
        if not is_due(now, root):
            await _sleep(3600)
            continue
        await _base_job(state, now)
        await _sleep(BASE_BACKUP_RETRY_INTERVAL_S)


async def _restore_job(state: BaseCandidateState) -> None:
    """One restore proof; a space deferral is not a failed proof."""
    state.restore_running = True
    try:
        with _claim_scheduler_ownership():
            inputs = _restore_worker_input()
            outcome = await run_restore_input(
                inputs
            )  # async-blocking-ok: ownership lock spans child proof
            candidate = CandidateManifest.from_json(inputs.candidate_json)
            publish(candidate, outcome)
        _record_protected(state, candidate)
    except OperationDeferred as exc:
        state.restore_error = str(exc)
        _log.info("restore proof deferred: %s", exc.detail)
    except Exception as exc:
        state.restore_error = str(exc)
        telemetry.emit(
            "telemetry",
            "recovery_drill_failed",
            level="error",
            attributes={"drill": "pitr", "detail": str(exc)},
        )
        _log.exception("restore proof failed; candidate remains unprotected")
    finally:
        state.restore_running = False


async def _base_job(state: BaseCandidateState, now: datetime) -> None:
    """One weekly base candidate; lock and space deferrals are not failures."""
    state.last_attempt = now.timestamp()
    state.running = True
    state.deferred_for_logical_backup = False
    try:
        with _claim_scheduler_ownership():
            await run_candidate()  # async-blocking-ok: ownership lock spans candidate child
        state.last_success = time.time()
        state.base_error = None
    except LockTimeoutError:
        state.deferred_for_logical_backup = True
        _log.info("base candidate deferred while logical backup owns backup lock")
    except OperationDeferred as exc:
        state.base_error = str(exc)
        _log.info("base candidate deferred: %s", exc.detail)
    except Exception as exc:
        state.base_error = str(exc)
        _log.exception("base candidate failed; retrying on bounded cadence")
    finally:
        state.running = False


async def run() -> None:
    pidfile = settings.services.pitr_base_backup_pidfile
    if not acquire_pidfile(pidfile, "services.pitr.base_scheduler_daemon"):
        return
    root = ava_home() / "physical-backup" / "base-manifests"
    physical_root = ava_home() / "physical-backup"
    cleanup_pending = (
        any((physical_root / "restore").glob(".*.partial"))
        or any((physical_root / "base-candidates").glob(".*.partial"))
        or any((physical_root / "base-candidates").glob(".*.retiring"))
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
    code = 0
    # `asyncio.Runner`, not `asyncio.run`: `run` closes in a `finally` that
    # awaits `shutdown_default_executor`, joining the default executor's
    # workers — the retention refresh among them — and a stop signal must never
    # wait on those (see `_hard_exit`). The runner is therefore never closed:
    # after the explicit drain below, teardown is skipped by the hard exit.
    runner = asyncio.Runner()
    try:
        runner.run(run())
    except KeyboardInterrupt:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a retry must not abort the bounded exit
        _log.info("[pitr-base-candidate] interrupted, shutting down")
        # The signal path skips Runner's own cancellation, so drain the loop's
        # tasks explicitly: run()'s finally still stops the health server and
        # removes the pidfile, and an in-flight candidate worker still reaps
        # its process group. The executor is deliberately NOT drained.
        failures = cancel_and_drain(runner)
        if failures:
            _log.error("[pitr-base-candidate] async shutdown failed: %r", failures)
            code = 1
    except Exception:
        _log.exception("PITR base candidate daemon crashed — uncaught exception escaped run()")
        code = 1
    _hard_exit(code)


if __name__ == "__main__":
    main()
