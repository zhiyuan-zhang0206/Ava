"""Daily OS job for copytruncate rotation followed by tiered retention.

Each cluster registers one local maintenance schedule. POSIX runs both commands
in one shell so retention starts only after rotation succeeds. Windows uses two
daily Task Scheduler jobs one minute apart because its windowless Python action
accepts one Ava argv rather than a shell pipeline.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from loguru import logger

import shared.host.system.cron

FAMILY_DAYS = "agent=15,shell=7,gateway=30,ops=30,watchdog=30,snapshot=7,other=3"
_CRON_MARKER = "# ava-logs-maintenance"
_HOUR = 4
_MINUTE = 40
_WINDOWS_TIME_LIMIT_S = 1800


def _label(slug: str) -> str:
    return f"{shared.host.system.cron.LAUNCHD_LABEL_PREFIX}.{slug}.logs-maintenance"


def _launchd_plist_path(slug: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label(slug)}.plist"


def _shell_command() -> str:
    ava = shlex.quote(shared.host.system.cron.ava_binary_path())
    return f"{ava} logs rotate && {ava} logs retention --family-days {FAMILY_DAYS}"


def _launchd_plist_content() -> str:
    slug = shared.host.system.cron._home_slug()
    log_file = Path(shared.host.system.cron.job_home()) / "logs" / "logs-maintenance.out.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_label(slug)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-c</string>
        <string>{escape(_shell_command())}</string>
    </array>
{shared.host.system.cron.launchd_env_block()}
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
    """Rewrite and reload this cluster's daily LaunchAgent."""
    slug = shared.host.system.cron._home_slug()
    label = _label(slug)
    plist_path = _launchd_plist_path(slug)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_launchd_plist_content(), encoding="utf-8")

    if shared.host.system.cron.reload_launchd_job(label, plist_path) != 0:
        return 1
    logger.info("launchd job '{}' loaded (daily at {:02d}:{:02d})", label, _HOUR, _MINUTE)
    return 0


def _unregister_macos(slug: str) -> int:
    shared.host.system.cron.remove_launchd_job(_label(slug), _launchd_plist_path(slug))
    return 0


def _cron_marker(slug: str) -> str:
    return f"{_CRON_MARKER}.{slug}"


def _register_linux() -> int:
    """Replace this cluster's 04:40 maintenance line in the user crontab."""
    missing_rc = shared.host.system.cron.require_crontab(
        "  * logs maintenance: crontab not installed; daily rotation and "
        "retention cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
    )
    if missing_rc is not None:
        return missing_rc

    slug = shared.host.system.cron._home_slug()
    marker = _cron_marker(slug)
    entry = (
        f"{_MINUTE} {_HOUR} * * * {shared.host.system.cron.cron_env_prefix()}"
        f"/bin/sh -c {shlex.quote(_shell_command())}  {marker}"
    )
    if shared.host.system.cron.replace_crontab_entry(
        marker,
        entry,
        skip_phrase="logs-maintenance registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
    ):
        return 1
    logger.info("crontab logs-maintenance entry added ({})", marker)
    return 0


def _unregister_linux(slug: str) -> int:
    return shared.host.system.cron.remove_crontab_entry(
        _cron_marker(slug), write_failure_rc=1, on_removed=None
    )


def _register_windows() -> str | None:
    """Register separate rotate and retention tasks at 04:40 and 04:41."""
    from shared.host.system.schtasks import create_daily_task

    failures: list[str] = []
    for kind, args, minute in (
        ("logs-rotate", ("logs", "rotate"), _MINUTE),
        (
            "logs-retention",
            ("logs", "retention", "--family-days", FAMILY_DAYS),
            _MINUTE + 1,
        ),
    ):
        reason = create_daily_task(
            kind,
            args,
            hour=_HOUR,
            minute=minute,
            time_limit_s=_WINDOWS_TIME_LIMIT_S,
        )
        if reason is not None:
            failures.append(f"{kind}: {reason}")
    return "; ".join(failures) or None


def _unregister_windows(slug: str) -> int:
    from shared.host.system.schtasks import delete_task

    delete_task("logs-rotate", slug)
    delete_task("logs-retention", slug)
    return 0


def register_logs_job() -> None:
    """Register this cluster's daily logs-maintenance job."""
    if not shared.host.system.cron.os_jobs_enabled():
        shared.host.system.cron.skip_os_job("logs-maintenance")
        return
    from shared.host.system.backend import get_backend

    get_backend().register_logs_job()


def unregister_logs_job(home: Path | None = None) -> None:
    """Remove a cluster's daily logs-maintenance job."""
    from shared.cluster import slug_for_home
    from shared.host.system.backend import get_backend

    get_backend().unregister_logs_job(slug_for_home(home))
