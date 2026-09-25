"""OS-scheduled registration for the orphan-hold watchdog (task #3887).

The `ava cluster hold-watchdog` command only helps if something runs it while
no in-cluster actor can: a full-stop shape kills the watchdogs, the ops
rounds, and the database itself, and leaves the platform scheduler as the
only live layer (the 2026-09-17 S3 blackout showed exactly that - launchd /
cron was what survived). This module registers ONE job per cluster home:

- macOS: a launchd LaunchAgent, ``StartInterval`` every
  ``interval_s`` seconds.
- Linux (incl. WSL): a user crontab line (minute granularity; the interval
  rounds up to whole minutes).
- Windows: a Task Scheduler ``/SC MINUTE`` task (minute granularity).

One job per HOME, not per capability as the watchdog probe is: the
maintenance hold is host-level state, so a box carrying two capabilities
completes it in one place. The command itself decides what to do (see
``shared.hold_watchdog``); the job is a periodic no-op until a hold is
provably orphaned.

The skeleton deliberately mirrors ``shared.os_watchdog_probe`` - same launchd
bootout-settle dance (its helpers are reused wholesale), shared crontab
read-modify-write primitives, same marker-scoped idempotence, same Windows degradation
contract (a registration failure warns loudly instead of failing converge).
What differs:

- The job command is ``cluster hold-watchdog`` with no ``--role``.
- The launchd label / plist is per home only.
- Windows carries a finite time limit. The ladder's legs are themselves
  bounded (the stop leg gets ``update_quiesce_timeout_seconds``; the start
  leg's readiness gate is ``SERVICE_READY_TIMEOUT_S``), and a wedged
  invocation under the scheduler's ``IgnoreNew`` policy blocks later ones -
  with the 72h default that is three days of no supervision. 30 minutes
  clears the slowest legitimate ladder with headroom while keeping a wedged
  run from blocking more than a few evaluation cycles at the 5-minute cadence.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from loguru import logger

from shared.os_cron import (
    LAUNCHD_LABEL_PREFIX,
    ava_binary_path,
    cron_env_prefix,
    launchd_env_block,
    os_jobs_enabled,
    remove_crontab_entry,
    replace_crontab_entry,
    require_crontab,
    skip_os_job,
)

#: Default schedule period. The evaluation itself is cheap (a handful of
#: local file/lock reads), and recovery latency is the completion bound plus
#: at most one period; 5 minutes trades a negligible duty cycle for that.
HOLD_WATCHDOG_INTERVAL_SECONDS = 300

_CRON_MARKER = "# ava-hold-watchdog"

# See the module docstring: the one bound the ladder must clear with headroom
# while staying far under the scheduler's 72h default.
_WINDOWS_TIME_LIMIT_S = 1800


def _home_slug() -> str:
    from shared.cluster import home_slug
    from shared.paths import ava_home

    return home_slug(ava_home())


def hold_watchdog_label(slug: str) -> str:
    """The launchd label for one cluster home's hold watchdog."""
    return f"{LAUNCHD_LABEL_PREFIX}.{slug}.hold-watchdog"


def _plist_path(slug: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{hold_watchdog_label(slug)}.plist"


def _log_file() -> Path:
    """The one log both job kinds append the watchdog's output to
    (launchd's StandardOutPath/StandardErrorPath; the crontab line's
    redirect). One function so the registrations cannot drift."""
    import shared.paths

    return shared.paths.ava_home() / "logs" / "hold-watchdog.log"


def _plist_content(interval_s: int) -> str:
    """launchd plist for the hold watchdog.

    ``RunAtLoad`` is false for the same reason as the watchdog probe's:
    registration happens inside converge, which ``ava start`` itself runs -
    firing the watchdog at that moment would evaluate a transition that is
    alive and well, and the first fire one interval later is late enough.
    """
    ava_path = ava_binary_path()
    log_file = _log_file()
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{hold_watchdog_label(_home_slug())}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{ava_path}</string>
        <string>cluster</string>
        <string>hold-watchdog</string>
    </array>
{launchd_env_block()}
    <key>StartInterval</key>
    <integer>{interval_s}</integer>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
</dict>
</plist>
"""


def _register_macos(interval_s: int) -> int:
    """Write + load the LaunchAgent. Idempotent."""
    # Reused from the watchdog probe: the launchd bootout-settle mechanics
    # (bootout then wait for removal before bootstrap) are subtle enough that
    # a second implementation would be a second bug.
    from shared.os_watchdog_probe import (
        launchctl,
        launchd_job_loaded,
        unload_launchd_job_before_bootstrap,
    )
    from shared.platform import descends_from_launchd_job, launchd_job_label

    slug = _home_slug()
    plist_path = _plist_path(slug)
    label = hold_watchdog_label(slug)
    service = f"gui/{os.getuid()}/{label}"
    content = _plist_content(interval_s)
    # The watchdog command's own ladder runs `ava start`, whose converge step
    # registers this very job - and bootout of our own ancestor would kill the
    # completion mid-flight. Defer like the probe: keep the old spec intact so
    # an external converge can still detect the pending change.
    if launchd_job_label() == label or descends_from_launchd_job(label):
        logger.info("Hold watchdog '{}' is registering itself - deferring reload", label)
        return 0
    loaded = launchd_job_loaded(service)
    if loaded and plist_path.exists() and plist_path.read_text() == content:
        return 0
    if loaded:
        unload_launchd_job_before_bootstrap(service)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(content)
    result = launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path))
    if result.returncode != 0:
        logger.error("launchctl bootstrap failed for {}: {}", label, result.stderr)
        return 1
    logger.info("launchd job '{}' loaded (every {}s)", label, interval_s)
    return 0


def _unregister_macos(slug: str) -> int:
    label = hold_watchdog_label(slug)
    subprocess.run(  # noqa: S603 — static repo-internal argv
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
    )
    plist_path = _plist_path(slug)
    if plist_path.exists():
        plist_path.unlink()
        logger.info("Removed hold-watchdog plist {}", plist_path)
    return 0


def _cron_marker(slug: str) -> str:
    return f"{_CRON_MARKER}.{slug}"


def _register_linux(interval_s: int) -> int:
    """Add this home's hold-watchdog line to the user's crontab. Idempotent.

    Same contract as the probe's Linux path: whole-minute granularity (the
    interval rounds UP), a host without crontab is a host that cannot provide
    this capability (warn and skip), and the line captures output into the
    job log the launchd plist also names.
    """
    missing_rc = require_crontab(
        "  ! hold watchdog: crontab not installed on this host (skipping); "
        "an orphaned maintenance hold will not be completed automatically",
        missing_returncode=0,
        missing_stream=sys.stdout,
    )
    if missing_rc is not None:
        return missing_rc

    minutes = max(1, interval_s // 60)
    marker = _cron_marker(_home_slug())
    log_file = _log_file()
    entry = (
        f"*/{minutes} * * * * mkdir -p {log_file.parent} && "
        f"{cron_env_prefix()}{ava_binary_path()} cluster hold-watchdog "
        f">> {log_file} 2>&1  {marker}"
    )

    if replace_crontab_entry(
        marker,
        entry,
        skip_phrase="hold-watchdog registration",
        update_failure=lambda err: logger.error("crontab update failed for hold watchdog: {}", err),
    ):
        return 1
    logger.info("crontab hold-watchdog entry added ({}, every {} min)", marker, minutes)
    return 0


def _unregister_linux(slug: str) -> int:
    marker = _cron_marker(slug)
    return remove_crontab_entry(
        marker,
        write_failure_rc=0,
        on_removed=lambda: logger.info("crontab hold-watchdog entry removed ({})", marker),
    )


def _register_windows(interval_s: int) -> str | None:
    """Register this home's hold watchdog as a Windows scheduled task."""
    from shared.os_schtasks import create_minute_task

    return create_minute_task(
        "hold-watchdog",
        ("cluster", "hold-watchdog"),
        interval_s // 60,
        time_limit_s=_WINDOWS_TIME_LIMIT_S,
    )


def _unregister_windows(slug: str) -> int:
    from shared.os_schtasks import delete_task

    return delete_task("hold-watchdog", slug)


def register_hold_watchdog(interval_s: int | None = None) -> None:
    """Register this home's OS-scheduled hold watchdog.

    Platform-aware — delegates to ``PlatformBackend.register_hold_watchdog``.
    Idempotent. A no-op when ``os_jobs_enabled()`` is off (the test suite).
    ``interval_s`` defaults to the ``hold_watchdog_interval_seconds`` setting.

    Raises:
        RuntimeError: on registration failure (POSIX). The Windows backend
        degrades to a loud warning instead.
    """
    if interval_s is None:
        from shared.config import settings

        interval_s = int(settings.gateway.hold_watchdog_interval_seconds)
    if not os_jobs_enabled():
        skip_os_job("hold-watchdog")
        return
    from shared.platform_backend import get_backend

    get_backend().register_hold_watchdog(interval_s)


def unregister_hold_watchdog(home: Path | None = None) -> None:
    """Remove this home's hold-watchdog job.

    `home` selects WHICH cluster's job to remove; it defaults to this
    process's own home (same contract as the watchdog probe's unregister).
    Safe to call when nothing is registered.
    """
    from shared.cluster import slug_for_home
    from shared.platform_backend import get_backend

    get_backend().unregister_hold_watchdog(slug_for_home(home))
