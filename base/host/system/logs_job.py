"""Daily OS job for copytruncate rotation followed by tiered retention.

The host registers one local maintenance schedule. POSIX runs both commands
in one shell so retention starts only after rotation succeeds.
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

from loguru import logger

import base.host.system.cron

FAMILY_DAYS = "agent=15,shell=7,gateway=30,ops=30,watchdog=30,snapshot=7,other=3"
_CRON_MARKER = "# ava-logs-maintenance"
_HOUR = 4
_MINUTE = 40


_LABEL = f"{base.host.system.cron.LAUNCHD_LABEL_PREFIX}.logs-maintenance"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _shell_command() -> str:
    ava = shlex.quote(base.host.system.cron.ava_binary_path())
    return f"{ava} logs rotate && {ava} logs retention --family-days {FAMILY_DAYS}"


def _launchd_plist_content() -> str:
    log_file = Path(base.host.system.cron.job_home()) / "logs" / "logs-maintenance.out.log"
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
            <integer>{_HOUR}</integer>
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
    label = _LABEL
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_launchd_plist_content(), encoding="utf-8")

    if base.host.system.cron.reload_launchd_job(label, plist_path) != 0:
        return 1
    logger.info("launchd job '{}' loaded (daily at {:02d}:{:02d})", label, _HOUR, _MINUTE)
    return 0


def _unregister_macos() -> int:
    base.host.system.cron.remove_launchd_job(_LABEL, _launchd_plist_path())
    return 0


def _register_linux() -> int:
    """Replace the 04:40 maintenance line in the user crontab.

    Lines are matched by the marker as a substring, so a line an older version
    wrote with a per-home suffix after the marker is replaced in place.
    """
    missing_rc = base.host.system.cron.require_crontab(
        "  * logs maintenance: crontab not installed; daily rotation and "
        "retention cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
    )
    if missing_rc is not None:
        return missing_rc

    marker = _CRON_MARKER
    entry = (
        f"{_MINUTE} {_HOUR} * * * {base.host.system.cron.cron_env_prefix()}"
        f"/bin/sh -c {shlex.quote(_shell_command())}  {marker}"
    )
    if base.host.system.cron.replace_crontab_entry(
        marker,
        entry,
        skip_phrase="logs-maintenance registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
    ):
        return 1
    logger.info("crontab logs-maintenance entry added ({})", marker)
    return 0


def _unregister_linux() -> int:
    return base.host.system.cron.remove_crontab_entry(
        _CRON_MARKER, write_failure_rc=1, on_removed=None
    )


def register_logs_job(*, enabled_reader: Callable[[], bool]) -> None:
    """Register the daily logs-maintenance job."""
    if not base.host.system.cron.os_jobs_enabled(enabled_reader=enabled_reader):
        base.host.system.cron.skip_os_job("logs-maintenance")
        return
    if not base.host.system.cron.owns_os_jobs("logs-maintenance"):
        return
    from base.host.system.backend import get_backend

    get_backend().register_logs_job()


def unregister_logs_job() -> None:
    """Remove the daily logs-maintenance job (a no-op outside the default home)."""
    if not base.host.system.cron.owns_os_jobs("logs-maintenance"):
        return
    from base.host.system.backend import get_backend

    get_backend().unregister_logs_job()
