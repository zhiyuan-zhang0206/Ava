"""Recurring OS job for the content-refresh pass (design §5.6; task #3267).

One schedule per host runs `meiva packages refresh --from-job` every
`MEIVA_PACKAGES_REFRESH_TICK_SECONDS` (default 900s). The command owns all the
gating (the jobs/refresh switches, a cluster update in flight, the per-home
flock, per-package cadence and backoff), so the job spec stays dumb and
idempotent — one tick, no re-registration when a policy changes.

POSIX surfaces the run's output in `$MEIVA_HOME/logs/packages-refresh.log`
(launchd appends stdout/stderr; the crontab line redirects).
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

_CRON_MARKER = "# meiva-packages-refresh"


_LABEL = f"{base.host.system.cron.LAUNCHD_LABEL_PREFIX}.packages-refresh"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _log_file() -> Path:
    return Path(base.host.system.cron.job_home()) / "logs" / "packages-refresh.log"


def _shell_command(*, host: Host) -> str:
    meiva = shlex.quote(base.host.system.cron.meiva_binary_path(host=host))
    return f"{meiva} packages refresh --from-job"


def _tick_seconds(*, tick_reader: Callable[[], int]) -> int:
    return tick_reader()


def launchd_plist_content(*, tick_reader: Callable[[], int], host: Host) -> str:
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
    <key>StartInterval</key>
    <integer>{_tick_seconds(tick_reader=tick_reader)}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
</dict>
</plist>
"""


def register_macos(*, tick_reader: Callable[[], int], host: Host) -> int:
    """Rewrite and reload the refresh LaunchAgent."""
    label = _LABEL
    plist_path = launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(
        launchd_plist_content(tick_reader=tick_reader, host=host), encoding="utf-8"
    )

    if base.host.system.cron.reload_launchd_job(label, plist_path, host=host) != 0:
        return 1
    logger.info(
        "launchd job '{}' loaded (every {}s)", label, _tick_seconds(tick_reader=tick_reader)
    )
    return 0


def unregister_macos(*, host: Host) -> int:
    base.host.system.cron.remove_launchd_job(_LABEL, launchd_plist_path(), host=host)
    return 0


def register_linux(*, tick_reader: Callable[[], int], host: Host) -> int:
    """Replace the refresh line in the user crontab.

    Lines are matched by the marker as a substring.
    """
    missing_rc = base.host.system.cron.require_crontab(
        "  * packages refresh: crontab not installed; the recurring refresh "
        "pass cannot be registered",
        missing_returncode=1,
        missing_stream=sys.stderr,
        host=host,
    )
    if missing_rc is not None:
        return missing_rc

    marker = _CRON_MARKER
    minutes = max(1, _tick_seconds(tick_reader=tick_reader) // 60)
    log_file = _log_file()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = (
        f"*/{minutes} * * * * {base.host.system.cron.cron_env_prefix()}"
        f"/bin/sh -c {shlex.quote(_shell_command(host=host))} "
        f">> {shlex.quote(str(log_file))} 2>&1  {marker}"
    )
    if base.host.system.cron.replace_crontab_entry(
        marker,
        entry,
        skip_phrase="packages-refresh registration",
        update_failure=lambda err: print(f"  * crontab update failed: {err}", file=sys.stderr),  # noqa: T201
        host=host,
    ):
        return 1
    logger.info("crontab packages-refresh entry added ({})", marker)
    return 0


def unregister_linux(*, host: Host) -> int:
    return base.host.system.cron.remove_crontab_entry(
        _CRON_MARKER, write_failure_rc=1, on_removed=None, host=host
    )


def register_packages_job(
    *,
    enabled_reader: Callable[[], bool],
    refresh_enabled_reader: Callable[[], bool],
    tick_reader: Callable[[], int],
    host: Host,
) -> None:
    """Register the recurring refresh pass (idempotent).

    Readers remain live through platform dispatch. The refresh switch is read
    after home ownership; the tick is read only while generating the job spec
    (and the existing macOS success log). No policy snapshot is retained.

    Skipped when OS jobs are off (`MEIVA_OS_JOBS_ENABLED`), when this home is not
    the default home (`owns_os_jobs`) or the refresh channel itself is off — the converge step is the only caller, so a disabled switch
    simply leaves no job behind on machines that never registered one.
    """
    if not base.host.system.cron.os_jobs_enabled(enabled_reader=enabled_reader):
        base.host.system.cron.skip_os_job("packages refresh")
        return
    if not base.host.system.cron.owns_os_jobs("packages refresh"):
        return
    if not refresh_enabled_reader():
        logger.info(
            "packages refresh disabled (MEIVA_PACKAGES_REFRESH_ENABLED=false) — not registering"
        )
        return
    base.host.system.cron.register_platform_job(
        "packages-refresh",
        macos=lambda: register_macos(tick_reader=tick_reader, host=host),
        linux=lambda: register_linux(tick_reader=tick_reader, host=host),
        host=host,
    )


def unregister_packages_job(*, host: Host) -> None:
    """Remove the recurring refresh pass; safe when none is registered, and a no-op
    outside the default home."""
    if not base.host.system.cron.owns_os_jobs("packages refresh"):
        return
    base.host.system.cron.unregister_platform_job(
        macos=lambda: unregister_macos(host=host),
        linux=lambda: unregister_linux(host=host),
        host=host,
    )
