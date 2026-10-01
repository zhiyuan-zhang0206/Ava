"""`ava backup walg`: check that WAL-G can work, and show what it is doing."""

from __future__ import annotations

import sys
from pathlib import Path

from base.cluster.dataplane import walg_binary
from services.gateway_side.walg import check, probe
from services.gateway_side.walg import config as walg_config
from services.gateway_side.walg.archive import expected_archive


def cmd_walg_check() -> int:
    """Run the pre-flight steps; the exit code is non-zero when any step failed."""
    steps = check.run_check()
    for step in steps:
        print(f"  {'✓' if step.ok else '✗'} {step.name}: {step.detail}")
    return 0 if all(step.ok for step in steps) else 1


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


def cmd_walg_status() -> int:
    """Print configuration, binary, key fingerprint and archiver facts; always exits 0."""
    path = walg_config.configured_path()
    if path is None:
        print("WAL-G archiving is off (AVA_WALG_CONFIG_FILE is not set)")
        return 0
    binary_problem = walg_binary.installed_problem()
    print(f"binary: {binary_problem or f'pinned wal-g {walg_binary.WALG_VERSION}'}")
    for line in (*_config_lines(path), *_postgres_lines()):
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
