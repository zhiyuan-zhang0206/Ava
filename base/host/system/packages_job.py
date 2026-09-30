"""Recurring OS job for the content-refresh pass (design §5.6; task #3267).

One schedule per host runs `ava packages refresh --from-job` every
`AVA_PACKAGES_REFRESH_TICK_SECONDS` (default 900s). The command owns all the
gating (the jobs/refresh switches, a cluster update in flight, the per-home
flock, per-package cadence and backoff), so the job spec stays dumb and
idempotent — one tick, no re-registration when a policy changes.

POSIX surfaces the run's output in `$AVA_HOME/logs/packages-refresh.log`
(launchd appends stdout/stderr; the crontab line redirects).
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from loguru import logger

import base.host.system.cron
from base.config import settings

_CRON_MARKER = "# ava-packages-refresh"


_LABEL = f"{base.host.system.cron.LAUNCHD_LABEL_PREFIX}.packages-refresh"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _log_file() -> Path:
    return Path(base.host.system.cron.job_home()) / "logs" / "packages-refresh.log"


def _shell_command() -> str:
    ava = shlex.quote(base.host.system.cron.ava_binary_path())
    return f"{ava} packages refresh --from-job"


def _tick_seconds() -> int:
    return settings.packages.refresh_tick_seconds


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
    """Rewrite and reload the refresh LaunchAgent."""
    label = _LABEL
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_launchd_plist_content(), encoding="utf-8")

    if base.host.system.cron.reload_launchd_job(label, plist_path) != 0:
        return 1
    logger.info("launchd job '{}' loaded (every {}s)", label, _tick_seconds())
    return 0


def _unregister_macos() -> int:
    base.host.system.cron.remove_launchd_job(_LABEL, _launchd_plist_path())
    return 0


def _register_linux() -> int:
    """Replace the refresh line in the user crontab.

    Lines are matched by the marker as a substring, so a line an older version
    wrote with a per-home suffix after the marker is replaced in place.
    """
    missing_rc = base.host.system.cron.require_crontab(
        "  * packages refresh: crontab not installed; the recurring refresh "
        "pass cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
    )
    if missing_rc is not None:
        return missing_rc

    marker = _CRON_MARKER
    minutes = max(1, _tick_seconds() // 60)
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = (
        f"*/{minutes} * * * * {base.host.system.cron.cron_env_prefix()}"
        f"/bin/sh -c {shlex.quote(_shell_command())} "
        f">> {shlex.quote(str(log_file))} 2>&1  {marker}"
    )
    if base.host.system.cron.replace_crontab_entry(
        marker,
        entry,
        skip_phrase="packages-refresh registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
    ):
        return 1
    logger.info("crontab packages-refresh entry added ({})", marker)
    return 0


def _unregister_linux() -> int:
    return base.host.system.cron.remove_crontab_entry(
        _CRON_MARKER, write_failure_rc=1, on_removed=None
    )


def register_packages_job() -> None:
    """Register the recurring refresh pass (idempotent).

    Skipped when OS jobs are off (`AVA_OS_JOBS_ENABLED`), when this home is not
    the default home (`owns_os_jobs`) or the refresh channel itself is off — the converge step is the only caller, so a disabled switch
    simply leaves no job behind on machines that never registered one.
    """
    if not base.host.system.cron.os_jobs_enabled():
        base.host.system.cron.skip_os_job("packages refresh")
        return
    if not base.host.system.cron.owns_os_jobs("packages refresh"):
        return
    if not settings.packages.refresh_enabled:
        logger.info(
            "packages refresh disabled (AVA_PACKAGES_REFRESH_ENABLED=false) — not registering"
        )
        return
    from base.host.system.backend import get_backend

    get_backend().register_packages_job()


def unregister_packages_job() -> None:
    """Remove the recurring refresh pass; safe when none is registered, and a no-op
    outside the default home."""
    if not base.host.system.cron.owns_os_jobs("packages refresh"):
        return
    from base.host.system.backend import get_backend

    get_backend().unregister_packages_job()
