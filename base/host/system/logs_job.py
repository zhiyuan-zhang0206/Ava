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
from base.host.system.hostport import Host

FAMILY_DAYS = "agent=15,shell=7,gateway=30,ops=30,watchdog=30,snapshot=7,other=3"
_CRON_MARKER = "# meiva-logs-maintenance"
_HOUR = 4
_MINUTE = 40


_LABEL = f"{base.host.system.cron.LAUNCHD_LABEL_PREFIX}.logs-maintenance"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _shell_command(*, host: Host) -> str:
    meiva = shlex.quote(base.host.system.cron.meiva_binary_path(host=host))
    return f"{meiva} logs rotate && {meiva} logs retention --family-days {FAMILY_DAYS}"


def launchd_plist_content(*, host: Host) -> str:
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
        <string>{escape(_shell_command(host=host))}</string>
    </array>
{base.host.system.cron.launchd_env_block(host=host)}
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


def register_macos(*, host: Host) -> int:
    """Rewrite and reload the daily LaunchAgent."""
    label = _LABEL
    plist_path = launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(launchd_plist_content(host=host), encoding="utf-8")

    if base.host.system.cron.reload_launchd_job(label, plist_path, host=host) != 0:
        return 1
    logger.info("launchd job '{}' loaded (daily at {:02d}:{:02d})", label, _HOUR, _MINUTE)
    return 0


def unregister_macos(*, host: Host) -> int:
    base.host.system.cron.remove_launchd_job(_LABEL, launchd_plist_path(), host=host)
    return 0


def register_linux(*, host: Host) -> int:
    """Replace the 04:40 maintenance line in the user crontab.

    Lines are matched by the marker as a substring.
    """
    missing_rc = base.host.system.cron.require_crontab(
        "  * logs maintenance: crontab not installed; daily rotation and "
        "retention cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
        host=host,
    )
    if missing_rc is not None:
        return missing_rc

    marker = _CRON_MARKER
    entry = (
        f"{_MINUTE} {_HOUR} * * * {base.host.system.cron.cron_env_prefix()}"
        f"/bin/sh -c {shlex.quote(_shell_command(host=host))}  {marker}"
    )
    if base.host.system.cron.replace_crontab_entry(
        marker,
        entry,
        skip_phrase="logs-maintenance registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
        host=host,
    ):
        return 1
    logger.info("crontab logs-maintenance entry added ({})", marker)
    return 0


def unregister_linux(*, host: Host) -> int:
    return base.host.system.cron.remove_crontab_entry(
        _CRON_MARKER, write_failure_rc=1, on_removed=None, host=host
    )


def register_logs_job(*, enabled_reader: Callable[[], bool], host: Host) -> None:
    """Register the daily logs-maintenance job."""
    if not base.host.system.cron.os_jobs_enabled(enabled_reader=enabled_reader):
        base.host.system.cron.skip_os_job("logs-maintenance")
        return
    if not base.host.system.cron.owns_os_jobs("logs-maintenance"):
        return
    base.host.system.cron.register_platform_job(
        "logs-maintenance",
        macos=lambda: register_macos(host=host),
        linux=lambda: register_linux(host=host),
        host=host,
    )


def unregister_logs_job(*, host: Host) -> None:
    """Remove the daily logs-maintenance job (a no-op outside the default home)."""
    if not base.host.system.cron.owns_os_jobs("logs-maintenance"):
        return
    base.host.system.cron.unregister_platform_job(
        macos=lambda: unregister_macos(host=host),
        linux=lambda: unregister_linux(host=host),
        host=host,
    )
