"""The daily WAL-G tick (`ava backup walg run`): back up, verify the chain, apply retention.

One command, run once a day by the OS job and by hand. It is safe to run at any
moment and any number of times:

1. **Gates.** Key unset: nothing to do. Another tick holds the per-home lock: that run
   is still going, nothing to do. A deploy window is open, or Postgres does not
   accept connections: the tick is *skipped* (recorded, exit 0) rather than failed, so
   a tick that happens to land inside a stop/start window does not become a day-long
   alert. A skip leaves the outcome of the last real run untouched.
2. **Preflight.** The pinned binary, the configuration and key pin, and one
   `backup-list`, which proves the credentials and the prefix.
3. **Recovery drill**, once a week (`drill.py`), before the backup, so that it restores
   yesterday's backup: the one a lost host would need. A failed drill is recorded in
   `drill` and never stops the backup.
4. **Backup.** `backup-push` with `WALG_DELTA_MAX_STEPS=6`: WAL-G itself starts a new
   full backup once the increment chain is six deep (a daily run therefore makes a
   full backup every seventh time), and retries a failed full backup the next day
   because the chain is still full. A backup that WAL-G reports done but that is not
   in the list afterwards is a failure.
5. **Verify.** `wal-verify integrity timeline --json`, JSON status only. A failure
   here stops the tick before retention.
6. **Retention.** `retain FULL 3`, guarded (see `retention.py`).

The first failing step ends the run and is recorded with its name; the next day's
tick starts from scratch. Every step's result is written to `state.json`, which the
health probe reads (`probe.py`). Output goes through `report`, one line at a time.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from datetime import UTC, datetime

import psycopg

from base.cluster.dataplane import walg_binary
from base.db import Database, pg_admin
from base.host.private_storage import ensure_private_dir
from base.log import logger
from base.native_process.os_platform import LockTimeoutError, file_lock
from services.gateway_side.walg import config as walg_config
from services.gateway_side.walg import drill, state
from services.gateway_side.walg.backups import Backup, BackupChainError, list_backups
from services.gateway_side.walg.pg_target import PgTarget, pg_target
from services.gateway_side.walg.retention import RetentionAbortedError, apply_retention
from services.gateway_side.walg.runner import WalgCommandError, run_walg
from services.gateway_side.walg.state import (
    STEP_BACKUP,
    STEP_PREFLIGHT,
    STEP_RETENTION,
    STEP_VERIFY,
    BackupRecord,
    RetentionRecord,
    RunRecord,
    TickRecord,
    VerifyRecord,
)
from services.gateway_side.walg.verify import VerifyOutputError, verify_chain

DELTA_MAX_STEPS = "6"
"""`WALG_DELTA_MAX_STEPS`: increments allowed before WAL-G takes a full backup (design
ruling: with a daily run, one full backup every seven runs)."""

BACKUP_TIMEOUT_S = 12 * 3600
"""Bound for `backup-push`: a base backup that has not finished in half a day is hung
(measured and extrapolated durations are well under two hours). A bound that keeps the
lock from being held forever, not an alert threshold."""

_CONNECT_TIMEOUT_S = 5

Report = Callable[[str], None]

# Failures whose message already says what went wrong; anything else is a bug and is
# recorded with its type as well.
_EXPECTED_FAILURES = (
    WalgCommandError,
    walg_config.WalgConfigError,
    BackupChainError,
    VerifyOutputError,
    RetentionAbortedError,
)


def postgres_accepts_connections(target: PgTarget) -> bool:
    """Whether the home's postmaster answers on its socket (and is the one that owns the data)."""
    try:
        with pg_admin.connect(
            target.admin_url,
            expected_data_dir=target.data_dir,
            autocommit=True,
            connect_timeout=_CONNECT_TIMEOUT_S,
        ):
            return True
    except psycopg.Error:
        return False


def deploy_window_reason(db: Database) -> str | None:
    """The sentence naming the open deploy window, or None."""
    from ops.deploy_window import deploy_in_flight

    window = deploy_in_flight(db)
    return window.detail if window.active else None


class StepFailedError(Exception):
    """A step failed: its name (for the alert) and the detail (for the log)."""

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


def _now() -> datetime:
    return datetime.now(UTC)


def _preflight() -> list[Backup]:
    problem = walg_binary.installed_problem()
    if problem is not None:
        raise StepFailedError(STEP_PREFLIGHT, f"{problem} (ava converge installs it)")
    try:
        walg_config.load_walg_config()
    except walg_config.WalgConfigError as exc:
        raise StepFailedError(STEP_PREFLIGHT, str(exc)) from None
    return list_backups()


def _backup(
    target: PgTarget, before: list[Backup], report: Report, now: Callable[[], datetime]
) -> list[Backup]:
    run_walg(
        ["backup-push", str(target.data_dir)],
        timeout_s=BACKUP_TIMEOUT_S,
        pg_admin_url=target.admin_url,
        extra_env={"WALG_DELTA_MAX_STEPS": DELTA_MAX_STEPS},
    )
    after = list_backups()
    if not after or (before and after[-1].name == before[-1].name):
        raise StepFailedError(STEP_BACKUP, "backup-push finished but no new backup is listed")
    newest = after[-1]
    kind = "full" if newest.is_full else "delta"
    state.update_state(
        backup=BackupRecord(
            name=newest.name,
            kind=kind,
            finished_at=now(),
            uncompressed_bytes=newest.uncompressed_bytes,
            compressed_bytes=newest.compressed_bytes,
        )
    )
    report(f"backup: {newest.name} ({kind}, {newest.compressed_bytes} bytes stored)")
    return after


def _drill(
    target: PgTarget, backups: list[Backup], report: Report, now: Callable[[], datetime]
) -> bool:
    """Drill the newest backup now; records the outcome. Returns whether it succeeded."""
    previous = state.read_state().drill
    record = drill.run_drill(target, backups[-1], previous, report, now)
    state.update_state(drill=record)
    if record.ok:
        report(f"drill: ok in {record.seconds:.0f}s ({record.detail})")
    else:
        report(f"drill: FAILED after {record.seconds:.0f}s: {record.detail}")
    return record.ok


def _weekly_drill(
    target: PgTarget, backups: list[Backup], report: Report, now: Callable[[], datetime]
) -> None:
    """The drill when one is due. Its failure is recorded and reported, never raised."""
    if drill.drill_due(state.read_state().drill, backups, now()):
        _drill(target, backups, report, now)


def _verify(target: PgTarget, report: Report, now: Callable[[], datetime]) -> str:
    verdict = verify_chain(target.admin_url)
    state.update_state(
        verify=VerifyRecord(at=now(), integrity=verdict.integrity, timeline=verdict.timeline)
    )
    summary = f"integrity {verdict.integrity}, timeline {verdict.timeline}"
    report(f"verify: {summary}")
    if verdict.failed:
        raise StepFailedError(STEP_VERIFY, f"the archived WAL chain is broken: {summary}")
    return summary


def _retention(backups: list[Backup], report: Report, now: Callable[[], datetime]) -> int:
    outcome = apply_retention(backups, report)
    state.update_state(
        retention=RetentionRecord(at=now(), marked=outcome.marked, deleted=outcome.deleted)
    )
    return outcome.deleted


def _run_steps(target: PgTarget, report: Report, now: Callable[[], datetime]) -> str:
    """The steps in order; returns the one-line summary of a successful run."""
    current = STEP_PREFLIGHT
    try:
        before = _preflight()
        report(f"preflight: ok, {len(before)} backups listed")
        _weekly_drill(target, before, report, now)
        current = STEP_BACKUP
        after = _backup(target, before, report, now)
        current = STEP_VERIFY
        chain = _verify(target, report, now)
        current = STEP_RETENTION
        deleted = _retention(after, report, now)
    except StepFailedError:
        raise
    except _EXPECTED_FAILURES as exc:
        raise StepFailedError(current, str(exc)) from None
    except Exception as exc:  # any other failure is still this step's, never a silent crash
        logger.error(f"[walg] tick step {current} failed: {traceback.format_exc()}")
        raise StepFailedError(current, f"{type(exc).__name__}: {exc}") from None
    return f"{after[-1].name}; chain {chain}; retention deleted {deleted}"


def _record_failure(
    started: datetime, failure: StepFailedError, report: Report, now: Callable[[], datetime]
) -> int:
    state.update_state(
        run=RunRecord(
            started_at=started,
            finished_at=now(),
            status="failed",
            step=failure.step,
            detail=failure.detail,
        )
    )
    report(f"failed at {failure.step}: {failure.detail}")
    return 1


def _skip_reason(db: Database, target: PgTarget) -> str | None:
    window = deploy_window_reason(db)
    if window is not None:
        return f"a deploy window is open ({window})"
    if not postgres_accepts_connections(target):
        return "postgres is not accepting connections"
    return None


def _locked_tick(db: Database, report: Report, now: Callable[[], datetime]) -> int:
    started = now()
    try:
        state.read_state()
    except state.StateError as exc:
        report(f"failed: {exc}; remove {state.state_path()} to reset it")
        return 1

    try:
        target = pg_target()
    except RuntimeError as exc:  # no locally owned Postgres to back up: a setup error
        return _record_failure(started, StepFailedError(STEP_PREFLIGHT, str(exc)), report, now)
    try:
        reason = _skip_reason(db, target)
    except RuntimeError as exc:  # the socket answers, but not as this home's Postgres
        return _record_failure(started, StepFailedError(STEP_PREFLIGHT, str(exc)), report, now)
    if reason is not None:
        state.update_state(tick=TickRecord(started_at=started, skipped=reason))
        report(f"skipped: {reason}")
        return 0

    state.update_state(tick=TickRecord(started_at=started))
    try:
        summary = _run_steps(target, report, now)
    except StepFailedError as failure:
        return _record_failure(started, failure, report, now)
    state.update_state(
        run=RunRecord(started_at=started, finished_at=now(), status="ok", step=None, detail=summary)
    )
    report(f"ok: {summary}")
    return 0


def run_tick(db: Database, report: Report, *, now: Callable[[], datetime] = _now) -> int:
    """Run one tick; the exit code is non-zero only when a step failed."""
    if not walg_config.enabled():
        report("WAL-G is off (AVA_WALG_CONFIG_FILE is not set); nothing to do")
        return 0
    ensure_private_dir(state.walg_dir())
    try:
        with file_lock(state.lock_path(), timeout_s=0):
            return _locked_tick(db, report, now)
    except LockTimeoutError:
        report("another WAL-G tick is still running; nothing to do")
        return 0


def _locked_drill(report: Report, now: Callable[[], datetime]) -> int:
    try:
        state.read_state()
        target = pg_target()
        if not postgres_accepts_connections(target):
            report("failed: postgres is not accepting connections")
            return 1
        backups = _preflight()
    except (state.StateError, RuntimeError, StepFailedError, *_EXPECTED_FAILURES) as exc:
        report(f"failed: {exc}")
        return 1
    if not backups:
        report("failed: no backup exists yet; run `ava backup walg run` first")
        return 1
    return 0 if _drill(target, backups, report, now) else 1


def run_drill_now(report: Report, *, now: Callable[[], datetime] = _now) -> int:
    """Run the recovery drill on the newest backup now (the weekly one runs inside the tick).

    Takes the same lock as the tick, so the two never overlap. Exit 0 only when the drill passed.
    """
    if not walg_config.enabled():
        report("WAL-G is off (AVA_WALG_CONFIG_FILE is not set); nothing to drill")
        return 1
    ensure_private_dir(state.walg_dir())
    try:
        with file_lock(state.lock_path(), timeout_s=0):
            return _locked_drill(report, now)
    except LockTimeoutError:
        report("failed: a WAL-G tick or drill is still running")
        return 1
