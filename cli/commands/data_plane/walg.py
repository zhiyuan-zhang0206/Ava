"""`ava backup walg`: check that WAL-G can work, run the daily tick, show what it is doing."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from base.cluster.dataplane import walg_binary
from services.gateway_side.walg import check, probe, state, tick
from services.gateway_side.walg import config as walg_config
from services.gateway_side.walg.archive import expected_archive


def cmd_walg_check() -> int:
    """Run the pre-flight steps; the exit code is non-zero when any step failed."""
    steps = check.run_check()
    for step in steps:
        print(f"  {'✓' if step.ok else '✗'} {step.name}: {step.detail}")
    return 0 if all(step.ok for step in steps) else 1


def cmd_walg_run() -> int:
    """Run one daily tick (backup, verify, retention); non-zero only when a step failed.

    The OS job runs exactly this, and so can an operator: concurrent runs stand down
    and a skipped or repeated run is harmless. Every output line carries a UTC
    timestamp because the job's output is appended to `$AVA_HOME/logs/walg.log`.
    """

    def report(line: str) -> None:
        print(f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {line}", flush=True)

    return tick.run_tick(report)


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


def _postgres_lines() -> list[str]:
    expected = expected_archive()
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
    return lines


def cmd_walg_status() -> int:
    """Print configuration, binary, key fingerprint, archiver and daily-tick facts; exits 0."""
    path = walg_config.configured_path()
    if path is None:
        print("WAL-G archiving is off (AVA_WALG_CONFIG_FILE is not set)")
        return 0
    binary_problem = walg_binary.installed_problem()
    print(f"binary: {binary_problem or f'pinned wal-g {walg_binary.WALG_VERSION}'}")
    for line in (*_config_lines(path), *_postgres_lines(), *_tick_lines()):
        print(line)
    failure = probe.failure()
    print(f"health: {failure or 'ok'}")
    return 0


def warn_archive_inactive() -> None:
    """Say so when WAL archiving is configured but this Postgres is not carrying it.

    A retained postmaster is only reloaded, so it keeps the launch arguments of its
    previous start: `archive_mode` and the archive command take effect at the next
    new launch. A warning, not a failure: the health probe is the alert.
    """
    expected = expected_archive()
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
