"""Daily OS job for the WAL-G physical backup (`meiva backup walg run`).

One schedule per host runs the daily tick: backup, verify the archived chain,
apply retention. The command owns every gate (the key switch, a deploy window,
Postgres reachability, the per-home lock), so the job spec stays a single dumb,
idempotent line: no re-registration when a policy changes.

The time of day is a scheduling choice, not a comparison threshold: it sits
`_HOURS_AFTER_LOGICAL_BACKUP` hours after the daily logical dump becomes due
(`services.backup_hour`) so the dump and its Sunday restore drill have finished
before a base backup starts reading the same disks, and it is not the 04:40 log
maintenance slot. Which hour that is follows the dump's hour; a changed
`MEIVA_BACKUP_HOUR` is picked up by the next converge.

POSIX surfaces the run's output in `$MEIVA_HOME/logs/walg.log` (launchd appends
stdout/stderr; the crontab line redirects).
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from loguru import logger

import base.host.system.cron
from base.host.system.hostport import Host

_CRON_MARKER = "# meiva-walg"
_HOURS_AFTER_LOGICAL_BACKUP = 3
MINUTE = 25

_LABEL = f"{base.host.system.cron.LAUNCHD_LABEL_PREFIX}.walg"


def hour(*, backup_hour_reader: Callable[[], int]) -> int:
    return (backup_hour_reader() + _HOURS_AFTER_LOGICAL_BACKUP) % 24


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _log_file() -> Path:
    return Path(base.host.system.cron.job_home()) / "logs" / "walg.log"


def _shell_command(*, host: Host) -> str:
    meiva = shlex.quote(base.host.system.cron.meiva_binary_path(host=host))
    return f"{meiva} backup walg run"


def launchd_plist_content(*, backup_hour_reader: Callable[[], int], host: Host) -> str:
    log_file = _log_file()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>{escape(_shell_command(host=host))}</string>
    </array>
{base.host.system.cron.launchd_env_block(host=host)}
    <key>StartCalendarInterval</key>
    <dict>
            <key>Hour</key>
            <integer>{hour(backup_hour_reader=backup_hour_reader)}</integer>
            <key>Minute</key>
            <integer>{MINUTE}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
</dict>
</plist>
"""


def register_macos(*, backup_hour_reader: Callable[[], int], host: Host) -> int:
    """Rewrite and reload the daily LaunchAgent."""
    plist_path = launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    _log_file().parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(
        launchd_plist_content(backup_hour_reader=backup_hour_reader, host=host), encoding="utf-8"
    )

    if base.host.system.cron.reload_launchd_job(_LABEL, plist_path, host=host) != 0:
        return 1
    logger.info(
        "launchd job '{}' loaded (daily at {:02d}:{:02d})",
        _LABEL,
        hour(backup_hour_reader=backup_hour_reader),
        MINUTE,
    )
    return 0


def unregister_macos(*, host: Host) -> int:
    base.host.system.cron.remove_launchd_job(_LABEL, launchd_plist_path(), host=host)
    return 0


def register_linux(*, backup_hour_reader: Callable[[], int], host: Host) -> int:
    """Replace the WAL-G tick line in the user crontab.

    Lines are matched by the marker as a substring, so a line with a suffix after
    the marker is replaced in place.
    """
    missing_rc = base.host.system.cron.require_crontab(
        "  * WAL-G backup: crontab not installed; the daily tick cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
        host=host,
    )
    if missing_rc is not None:
        return missing_rc

    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = (
        f"{MINUTE} {hour(backup_hour_reader=backup_hour_reader)} * * * {base.host.system.cron.cron_env_prefix()}"
        f"/bin/sh -c {shlex.quote(_shell_command(host=host))} "
        f">> {shlex.quote(str(log_file))} 2>&1  {_CRON_MARKER}"
    )
    if base.host.system.cron.replace_crontab_entry(
        _CRON_MARKER,
        entry,
        skip_phrase="WAL-G tick registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
        host=host,
    ):
        return 1
    logger.info("crontab WAL-G tick entry added ({})", _CRON_MARKER)
    return 0


def unregister_linux(*, host: Host) -> int:
    return base.host.system.cron.remove_crontab_entry(
        _CRON_MARKER, write_failure_rc=1, on_removed=None, host=host
    )


def register_walg_job(
    *, enabled_reader: Callable[[], bool], backup_hour_reader: Callable[[], int], host: Host
) -> None:
    """Register the daily WAL-G tick (idempotent).

    The composition root supplies live readers. The backup hour reaches the
    platform unchanged and is read while generating the job spec and the
    existing macOS success log, preserving the three-hour offset.

    Skipped when OS jobs are off (`MEIVA_OS_JOBS_ENABLED`) or this home is not the
    default home (`owns_os_jobs`). Whether WAL-G is switched on is the converge
    step's decision (`ensure_walg_job`), not this registrar's.
    """
    if not base.host.system.cron.os_jobs_enabled(enabled_reader=enabled_reader):
        base.host.system.cron.skip_os_job("WAL-G tick")
        return
    if not base.host.system.cron.owns_os_jobs("WAL-G tick"):
        return
    base.host.system.cron.register_platform_job(
        "WAL-G tick",
        macos=lambda: register_macos(backup_hour_reader=backup_hour_reader, host=host),
        linux=lambda: register_linux(backup_hour_reader=backup_hour_reader, host=host),
        host=host,
    )


def unregister_walg_job(*, host: Host) -> None:
    """Remove the daily WAL-G tick; safe when none is registered, and a no-op
    outside the default home."""
    if not base.host.system.cron.owns_os_jobs("WAL-G tick"):
        return
    base.host.system.cron.unregister_platform_job(
        macos=lambda: unregister_macos(host=host),
        linux=lambda: unregister_linux(host=host),
        host=host,
    )
