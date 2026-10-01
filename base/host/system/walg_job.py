"""Daily OS job for the WAL-G physical backup (`ava backup walg run`).

One schedule per host runs the daily tick: backup, verify the archived chain,
apply retention. The command owns every gate (the key switch, a deploy window,
Postgres reachability, the per-home lock), so the job spec stays a single dumb,
idempotent line: no re-registration when a policy changes.

The time of day is a scheduling choice, not a comparison threshold: it sits
`_HOURS_AFTER_LOGICAL_BACKUP` hours after the daily logical dump becomes due
(`services.backup_hour`) so the dump and its Sunday restore drill have finished
before a base backup starts reading the same disks, and it is not the 04:40 log
maintenance slot. Which hour that is follows the dump's hour; a changed
`AVA_BACKUP_HOUR` is picked up by the next converge.

POSIX surfaces the run's output in `$AVA_HOME/logs/walg.log` (launchd appends
stdout/stderr; the crontab line redirects).
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from loguru import logger

import base.host.system.cron
from base.config import settings

_CRON_MARKER = "# ava-walg"
_HOURS_AFTER_LOGICAL_BACKUP = 3
_MINUTE = 25

_LABEL = f"{base.host.system.cron.LAUNCHD_LABEL_PREFIX}.walg"


def _hour() -> int:
    return (settings.services.backup_hour + _HOURS_AFTER_LOGICAL_BACKUP) % 24


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _log_file() -> Path:
    return Path(base.host.system.cron.job_home()) / "logs" / "walg.log"


def _shell_command() -> str:
    ava = shlex.quote(base.host.system.cron.ava_binary_path())
    return f"{ava} backup walg run"


def _launchd_plist_content() -> str:
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
        <string>{escape(_shell_command())}</string>
    </array>
{base.host.system.cron.launchd_env_block()}
    <key>StartCalendarInterval</key>
    <dict>
            <key>Hour</key>
            <integer>{_hour()}</integer>
            <key>Minute</key>
            <integer>{_MINUTE}</integer>
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


def _register_macos() -> int:
    """Rewrite and reload the daily LaunchAgent."""
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    _log_file().parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_launchd_plist_content(), encoding="utf-8")

    if base.host.system.cron.reload_launchd_job(_LABEL, plist_path) != 0:
        return 1
    logger.info("launchd job '{}' loaded (daily at {:02d}:{:02d})", _LABEL, _hour(), _MINUTE)
    return 0


def _unregister_macos() -> int:
    base.host.system.cron.remove_launchd_job(_LABEL, _launchd_plist_path())
    return 0


def _register_linux() -> int:
    """Replace the WAL-G tick line in the user crontab.

    Lines are matched by the marker as a substring, so a line with a suffix after
    the marker is replaced in place.
    """
    missing_rc = base.host.system.cron.require_crontab(
        "  * WAL-G backup: crontab not installed; the daily tick cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
    )
    if missing_rc is not None:
        return missing_rc

    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = (
        f"{_MINUTE} {_hour()} * * * {base.host.system.cron.cron_env_prefix()}"
        f"/bin/sh -c {shlex.quote(_shell_command())} "
        f">> {shlex.quote(str(log_file))} 2>&1  {_CRON_MARKER}"
    )
    if base.host.system.cron.replace_crontab_entry(
        _CRON_MARKER,
        entry,
        skip_phrase="WAL-G tick registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
    ):
        return 1
    logger.info("crontab WAL-G tick entry added ({})", _CRON_MARKER)
    return 0


def _unregister_linux() -> int:
    return base.host.system.cron.remove_crontab_entry(
        _CRON_MARKER, write_failure_rc=1, on_removed=None
    )


def register_walg_job() -> None:
    """Register the daily WAL-G tick (idempotent).

    Skipped when OS jobs are off (`AVA_OS_JOBS_ENABLED`) or this home is not the
    default home (`owns_os_jobs`). Whether WAL-G is switched on is the converge
    step's decision (`ensure_walg_job`), not this registrar's.
    """
    if not base.host.system.cron.os_jobs_enabled():
        base.host.system.cron.skip_os_job("WAL-G tick")
        return
    if not base.host.system.cron.owns_os_jobs("WAL-G tick"):
        return
    from base.host.system.backend import get_backend

    get_backend().register_walg_job()


def unregister_walg_job() -> None:
    """Remove the daily WAL-G tick; safe when none is registered, and a no-op
    outside the default home."""
    if not base.host.system.cron.owns_os_jobs("WAL-G tick"):
        return
    from base.host.system.backend import get_backend

    get_backend().unregister_walg_job()
