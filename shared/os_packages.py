"""Recurring OS job for the content-refresh pass (design §5.6; task #3267).

One schedule per cluster runs `ava packages refresh --from-job` every
`AVA_PACKAGES_REFRESH_TICK_SECONDS` (default 900s). The command owns all the
gating (the jobs/refresh switches, a cluster update in flight, the per-home
flock, per-package cadence and backoff), so the job spec stays dumb and
idempotent — one tick, no re-registration when a policy changes.

POSIX surfaces the run's output in `$AVA_HOME/logs/packages-refresh.log`
(launchd appends stdout/stderr; the crontab line redirects). Windows registers
a minute-interval Task Scheduler job and degrades a registration failure to a
loud warning, like its sibling jobs (`shared.platform_backend`).
"""

from __future__ import annotations

import shlex
from pathlib import Path
from xml.sax.saxutils import escape

from loguru import logger

import shared.os_cron
from shared.config import settings

_CRON_MARKER = "# ava-packages-refresh"
_WINDOWS_TIME_LIMIT_S = 900


def _label(slug: str) -> str:
    return f"{shared.os_cron.LAUNCHD_LABEL_PREFIX}.{slug}.packages-refresh"


def _launchd_plist_path(slug: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label(slug)}.plist"


def _log_file() -> Path:
    return Path(shared.os_cron.job_home()) / "logs" / "packages-refresh.log"


def _shell_command() -> str:
    ava = shlex.quote(shared.os_cron.ava_binary_path())
    return f"{ava} packages refresh --from-job"


def _tick_seconds() -> int:
    return settings.packages.refresh_tick_seconds


def _launchd_plist_content() -> str:
    slug = shared.os_cron._home_slug()
    log_file = _log_file()
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
{shared.os_cron.launchd_env_block()}
    <key>StartInterval</key>
    <integer>{_tick_seconds()}</integer>
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
    """Rewrite and reload this cluster's refresh LaunchAgent."""
    slug = shared.os_cron._home_slug()
    label = _label(slug)
    plist_path = _launchd_plist_path(slug)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_launchd_plist_content(), encoding="utf-8")

    if shared.os_cron.reload_launchd_job(label, plist_path) != 0:
        return 1
    logger.info("launchd job '{}' loaded (every {}s)", label, _tick_seconds())
    return 0


def _unregister_macos(slug: str) -> int:
    shared.os_cron.remove_launchd_job(_label(slug), _launchd_plist_path(slug))
    return 0


def _cron_marker(slug: str) -> str:
    return f"{_CRON_MARKER}.{slug}"


def _register_linux() -> int:
    """Replace this cluster's refresh line in the user crontab."""
    missing_rc = shared.os_cron.require_crontab(
        "  * packages refresh: crontab not installed; the recurring refresh "
        "pass cannot be registered",
        missing_returncode=1,
    )
    if missing_rc is not None:
        return missing_rc

    slug = shared.os_cron._home_slug()
    marker = _cron_marker(slug)
    minutes = max(1, _tick_seconds() // 60)
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = (
        f"*/{minutes} * * * * {shared.os_cron.cron_env_prefix()}"
        f"/bin/sh -c {shlex.quote(_shell_command())} "
        f">> {shlex.quote(str(log_file))} 2>&1  {marker}"
    )
    if shared.os_cron.replace_crontab_entry(marker, entry, registration_name="packages-refresh"):
        return 1
    logger.info("crontab packages-refresh entry added ({})", marker)
    return 0


def _unregister_linux(slug: str) -> int:
    return shared.os_cron.remove_crontab_entry(_cron_marker(slug))


def _register_windows() -> str | None:
    """Register the minute-interval refresh task."""
    from shared.os_schtasks import create_minute_task

    minutes = max(1, _tick_seconds() // 60)
    return create_minute_task(
        "packages-refresh",
        ("packages", "refresh", "--from-job"),
        minutes,
        time_limit_s=_WINDOWS_TIME_LIMIT_S,
    )


def _unregister_windows(slug: str) -> int:
    from shared.os_schtasks import delete_task

    delete_task("packages-refresh", slug)
    return 0


def register_packages_job() -> None:
    """Register this cluster's recurring refresh pass (idempotent).

    Skipped when OS jobs are off (`AVA_OS_JOBS_ENABLED`) or the refresh channel
    itself is off — the converge step is the only caller, so a disabled switch
    simply leaves no job behind on machines that never registered one.
    """
    if not shared.os_cron.os_jobs_enabled():
        shared.os_cron.skip_os_job("packages refresh")
        return
    if not settings.packages.refresh_enabled:
        logger.info(
            "packages refresh disabled (AVA_PACKAGES_REFRESH_ENABLED=false) — not registering"
        )
        return
    from shared.platform_backend import get_backend

    get_backend().register_packages_job()


def unregister_packages_job(home: Path | None = None) -> None:
    """Remove a cluster's recurring refresh pass; safe when none is registered."""
    from shared.cluster import slug_for_home
    from shared.platform_backend import get_backend

    get_backend().unregister_packages_job(slug_for_home(home))
