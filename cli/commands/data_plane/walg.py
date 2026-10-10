"""`ava backup walg`: check that WAL-G can work, run the daily tick, restore, show what it is doing."""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from base.cluster.dataplane import walg_binary
from base.config import ConfigBoot
from base.db import Database
from services.backup.walg import check, probe, state, tick
from services.backup.walg import config as walg_config
from services.backup.walg.archive import expected_archive
from services.backup.walg.restore import RecoveryTarget, RestoreError, restored_instance


def cmd_walg_check() -> int:
    """Run the pre-flight steps; the exit code is non-zero when any step failed."""
    config = ConfigBoot()

    def path_reader() -> Path | None:
        if not config.prepared:
            config.read_process_environment()
        return config.view.walg.walg_config_file

    steps = check.run_check(path_reader=path_reader)
    for step in steps:
        print(f"  {'✓' if step.ok else '✗'} {step.name}: {step.detail}")
    return 0 if all(step.ok for step in steps) else 1


def _stamped(line: str) -> None:
    """One output line with a UTC timestamp: the OS job's output is appended to a log file."""
    print(f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {line}", flush=True)


def cmd_walg_run() -> int:
    """Run one daily tick (backup, verify, retention); non-zero only when a step failed.

    The OS job runs exactly this, and so can an operator: concurrent runs stand down
    and a skipped or repeated run is harmless.
    """
    config = ConfigBoot()

    def path_reader() -> Path | None:
        if not config.prepared:
            config.read_process_environment()
        return config.view.walg.walg_config_file

    return tick.run_tick(Database.from_settings(), _stamped, path_reader=path_reader)


def cmd_walg_drill() -> int:
    """Run the recovery drill on the newest backup now; non-zero unless it passed.

    The tick runs the same drill once a week before its backup; a success here counts
    as that week's drill.
    """
    config = ConfigBoot()

    def path_reader() -> Path | None:
        if not config.prepared:
            config.read_process_environment()
        return config.view.walg.walg_config_file

    return tick.run_drill_now(_stamped, path_reader=path_reader)


def cmd_walg_restore(
    *, directory: str, backup: str, time: str | None, lsn: str | None, user: str | None = None
) -> int:
    """Restore a backup into an empty directory and recover it to the target.

    The directory ends as a promoted database, its scratch Postgres shut down: it is
    not started, and it is never this home's live data directory. `user` is the restored
    cluster's superuser (the OS user that ran initdb on the source; default: this OS user).
    """
    config = ConfigBoot()

    def path_reader() -> Path | None:
        if not config.prepared:
            config.read_process_environment()
        return config.view.walg.walg_config_file

    if not walg_config.enabled(path_reader=path_reader):
        print("WAL-G is off (AVA_WALG_CONFIG_FILE is not set); nothing to restore from")
        return 1
    binary_problem = walg_binary.installed_problem()
    if binary_problem is not None:
        print(f"restore failed: {binary_problem} (ava converge installs it)", file=sys.stderr)
        return 1
    try:
        walg_config.load_walg_config(path_reader=path_reader)
        target = RecoveryTarget(time=time, lsn=lsn)
    except (walg_config.WalgConfigError, ValueError) as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    try:
        with restored_instance(
            Path(directory).resolve(),
            backup=backup,
            target=target,
            report=_stamped,
            user=user,
            keep_data=True,
            path_reader=path_reader,
        ):
            pass
    except RestoreError as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        print(f"remove any partly restored content in {directory} before retrying", file=sys.stderr)
        return 1
    print(
        f"restored: {directory} holds the recovered, promoted database; Postgres is not running on it"
    )
    return 0


def _config_lines(path: Path) -> list[str]:
    lines = [f"config file: {path}"]
    try:
        config = walg_config.read_config(path)
    except walg_config.WalgConfigError as exc:
        return [*lines, f"configuration: UNUSABLE: {exc}"]
    pinned = walg_config.pinned_key_id()
    lines.append(f"prefix: {config.prefix}")
    lines.append(f"key fingerprint: {config.key_fingerprint} (pinned: {pinned or 'not yet'})")
    problem = walg_config.pin_problem(config)
    if problem is not None:
        lines.append(f"KEY MISMATCH: {problem}")
    return lines


def _postgres_lines(*, path_reader: Callable[[], Path | None]) -> list[str]:
    expected = expected_archive(path_reader=path_reader)
    try:
        with probe.admin_connection() as conn:
            state = probe.read_archiver_state(conn)
    except Exception as exc:
        return [f"postgres: archiver state not read ({type(exc).__name__}: {exc})"]
    differs = expected is not None and probe.settings_differ(state, expected)
    pending = (
        "none pending"
        if state.oldest_pending_age_s is None
        else f"oldest pending {state.oldest_pending_age_s:.0f}s"
    )
    return [
        f"postgres: archive_mode={state.archive_mode}"
        + (" (differs from the configuration: ava stop, then ava start)" if differs else ""),
        f"archiver: archived={state.archived_count} failed={state.failed_count} "
        f"failing_now={state.failing_now}, {pending}",
    ]


def _tick_lines() -> list[str]:
    try:
        recorded = state.read_state()
    except state.StateError as exc:
        return [f"daily tick: state UNREADABLE: {exc}"]
    if recorded.tick is None:
        return ["daily tick: never ran"]
    lines = [f"daily tick: last started {recorded.tick.started_at.isoformat()}"]
    if recorded.tick.skipped is not None:
        lines[0] += f", skipped ({recorded.tick.skipped})"
    if recorded.run is not None:
        failed_at = f" at {recorded.run.step}" if recorded.run.status == "failed" else ""
        lines.append(
            f"last run: {recorded.run.status}{failed_at} "
            f"({recorded.run.finished_at.isoformat()}): {recorded.run.detail}"
        )
    if recorded.backup is not None:
        lines.append(
            f"last backup: {recorded.backup.name} ({recorded.backup.kind}, "
            f"{recorded.backup.finished_at.isoformat()})"
        )
    if recorded.verify is not None:
        lines.append(
            f"last verify: integrity {recorded.verify.integrity}, "
            f"timeline {recorded.verify.timeline} ({recorded.verify.at.isoformat()})"
        )
    if recorded.retention is not None:
        lines.append(
            f"last retention: {recorded.retention.deleted} objects deleted "
            f"({recorded.retention.at.isoformat()})"
        )
    if recorded.drill is not None:
        drill = recorded.drill
        target = drill.target_lsn or "the end of the archive"
        lines.append(
            f"last drill: {'ok' if drill.ok else 'FAILED'} ({drill.finished_at.isoformat()}): "
            f"{drill.backup} to {target} in {drill.seconds:.0f}s; {drill.detail}"
        )
        last_ok = drill.last_ok_at.isoformat() if drill.last_ok_at is not None else "never"
        lines.append(f"last successful drill: {last_ok}")
    return lines


def cmd_walg_status() -> int:
    """Print configuration, binary, key fingerprint, archiver, daily-tick and drill facts; exits 0."""
    config = ConfigBoot()

    def path_reader() -> Path | None:
        if not config.prepared:
            config.read_process_environment()
        return config.view.walg.walg_config_file

    path = walg_config.configured_path(path_reader=path_reader)
    if path is None:
        print("WAL-G archiving is off (AVA_WALG_CONFIG_FILE is not set)")
        return 0
    binary_problem = walg_binary.installed_problem()
    print(f"binary: {binary_problem or f'pinned wal-g {walg_binary.WALG_VERSION}'}")
    for line in (*_config_lines(path), *_postgres_lines(path_reader=path_reader), *_tick_lines()):
        print(line)
    failure = probe.failure(path_reader=path_reader)
    print(f"health: {failure or 'ok'}")
    return 0


def warn_archive_inactive(*, path_reader: Callable[[], Path | None]) -> None:
    """Say so when WAL archiving is configured but this Postgres is not carrying it.

    A retained postmaster is only reloaded, so it keeps the launch arguments of its
    previous start: `archive_mode` and the archive command take effect at the next
    new launch. A warning, not a failure: the health probe is the alert.
    """
    expected = expected_archive(path_reader=path_reader)
    if expected is None:
        return
    try:
        with probe.admin_connection() as conn:
            state = probe.read_archiver_state(conn)
    except Exception as exc:
        print(f"  ! WAL archiving state not read ({type(exc).__name__}: {exc})", file=sys.stderr)
        return
    if probe.settings_differ(state, expected):
        print(
            "  ! WAL archiving is configured but this Postgres is not running with the "
            "configured archive settings (it kept its previous launch arguments): run "
            "`ava stop` and `ava start` to activate it",
            file=sys.stderr,
        )
