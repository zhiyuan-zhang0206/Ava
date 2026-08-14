"""OS-scheduled liveness probe that revives a dead per-capability watchdog.

The watchdog daemon (``services/watchdog/daemon.py``) is what keeps every other
service alive: each round it runs its capability's healthchecks and respawns
whatever died. Nothing kept the *watchdog itself* alive. Its module docstring
named this and accepted it — "if the watchdog dies the user manually runs
``ava start`` to revive ... for a single-user system, this self-describing
recursion is acceptable".

That assumption does not survive a multi-machine fleet. Observed on the WSL
agent-runner: its watchdog died at 20:21, and its restarter / ops / browser
sessions stayed down for hours with nobody watching. The boot autostart job
(``shared.os_autostart``) could not help — ``@reboot`` / ``RunAtLoad`` fire once
at boot, and that box had been up for four days.

So the recursion terminates in the OS scheduler, which really is externally
supervised (launchd is pid-1-adjacent; crond is revived by init). This module
registers ONE job per capability the host carries, each running
``ava cluster watchdog-probe --role <role>`` every
``WATCHDOG_PROBE_INTERVAL_SECONDS``.

Deliberately NOT routed through the ops controller-manager gates (pause / schema
/ pin) that ``_tick`` consults: those gates *live inside the watchdog process*,
so making the revival of a dead watchdog wait on them would be circular. The
probe is dumb on purpose — alive, do nothing; dead, respawn — which matches how
launchd / cron already behave (each round an independent fork, no shared state,
a failed round just drops).

- macOS: launchd User LaunchAgent, ``StartInterval`` (arbitrary seconds).
- Linux (incl. WSL): user crontab, ``* * * * *`` (minute granularity — the
  interval is rounded up to whole minutes, see ``_register_linux``).
- Windows: Task Scheduler, ``/SC MINUTE`` (minute granularity, same rounding).

Lives in the shared layer so the CLI converge step can register the job without
violating the import layering (shared < ava < agent < gateway < cli).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from loguru import logger

from shared.config import settings
from shared.machine import MachineRole
from shared.os_cron import (
    LAUNCHD_LABEL_PREFIX,
    ava_binary_path,
    cron_env_prefix,
    launchd_env_block,
    os_jobs_enabled,
    skip_os_job,
)
from shared.platform import crontab_lock

# One minute. The watchdog's own round is 60s, so probing faster would only
# shorten the window in which a *dead* watchdog goes unnoticed, not the window
# in which a dead *service* does — the revived watchdog still needs its own tick
# to reach the services. Linux crontab cannot express sub-minute schedules at
# all, so a smaller value here would silently mean different things per platform.
WATCHDOG_PROBE_INTERVAL_SECONDS = 60

# Crontab marker: scoped by BOTH role and home slug, so a single box running two
# capabilities gets two independent lines, and a co-located second cluster never
# rewrites this one's.
_CRON_MARKER = "# ava-watchdog-probe"


def _home_slug() -> str:
    from shared.cluster import home_slug
    from shared.paths import ava_home

    return home_slug(ava_home())


def probe_label(role: str, slug: str) -> str:
    """launchd label for one cluster's watchdog probe for ``role``.

    ``com.ava.<home-slug>.watchdog-probe.<role>`` — the slug keeps two clusters
    sharing a home basename distinct, the role suffix keeps a single box's two
    capability jobs distinct.
    """
    return f"{LAUNCHD_LABEL_PREFIX}.{slug}.watchdog-probe.{role}"


def _plist_path(role: str, slug: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{probe_label(role, slug)}.plist"


def _plist_content(role: str, interval_s: int) -> str:
    """launchd plist for the watchdog probe.

    ``RunAtLoad`` is false: registration happens during converge, while
    ``ava start`` is still bringing services up. Firing the probe at that moment
    would race the start it is part of — the watchdog session may legitimately
    not exist yet — and the probe would respawn a watchdog ``ava start`` is about
    to spawn itself. The first fire one interval later is late enough.
    """
    ava_path = ava_binary_path()
    log_file = Path(settings.general.ava_home) / "logs" / "watchdog-probe.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{probe_label(role, _home_slug())}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{ava_path}</string>
        <string>cluster</string>
        <string>watchdog-probe</string>
        <string>--role</string>
        <string>{role}</string>
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


def _register_macos(role: str, interval_s: int) -> int:
    """Write + load the LaunchAgent for ``role``'s watchdog probe. Idempotent."""
    slug = _home_slug()
    plist_path = _plist_path(role, slug)
    label = probe_label(role, slug)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_plist_content(role, interval_s))

    # Bootout any existing instance so the reload picks up a changed interval /
    # ava path; a not-loaded job makes this a no-op.
    subprocess.run(  # noqa: S603
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
    )
    result = subprocess.run(  # noqa: S603
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.error("launchctl bootstrap failed for {}: {}", label, result.stderr)
        return 1
    logger.info("launchd job '{}' loaded (every {}s)", label, interval_s)
    return 0


def _unregister_macos(role: str, slug: str) -> int:
    plist_path = _plist_path(role, slug)
    label = probe_label(role, slug)
    subprocess.run(  # noqa: S603
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
    )
    if plist_path.exists():
        plist_path.unlink()
        logger.info("Removed watchdog-probe plist {}", plist_path)
    return 0


def _cron_marker(role: str, slug: str) -> str:
    return f"{_CRON_MARKER}.{role}.{slug}"


def _register_linux(role: str, interval_s: int) -> int:
    """Add this role's watchdog-probe line to the user's crontab. Idempotent.

    crontab granularity is whole minutes, so a sub-minute ``interval_s`` rounds
    UP to 1 minute rather than silently becoming "every minute" for a caller that
    asked for 5 seconds.

    A host without crontab (hermetic bench / CI container) is a host that cannot
    provide this capability: warn and skip rather than fail the whole bring-up,
    matching how ``os_cron`` / ``os_autostart`` degrade.
    """
    if shutil.which("crontab") is None:
        print(  # noqa: T201
            f"  ! watchdog probe ({role}): crontab not installed on this host (skipping); "
            "a dead watchdog will not be revived automatically"
        )
        return 0

    minutes = max(1, interval_s // 60)
    marker = _cron_marker(role, _home_slug())
    entry = (
        f"*/{minutes} * * * * {cron_env_prefix()}{ava_binary_path()} "
        f"cluster watchdog-probe --role {role}  {marker}"
    )

    with crontab_lock():
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if result.returncode != 0 and "no crontab" not in (result.stderr or "").lower():
            # Only the benign "no crontab for <user>" may be treated as an empty
            # crontab; anything else (permissions, a broken cron) would make the
            # rewrite below clobber the user's real crontab from "".
            print(  # noqa: T201
                f"  * crontab -l failed ({result.stderr.strip() or result.returncode}); "
                f"skipping watchdog-probe registration for {role} to avoid clobbering the crontab",
                file=sys.stderr,
            )
            return 1
        current = result.stdout if result.returncode == 0 else ""

        lines = [line for line in current.splitlines() if marker not in line]
        lines.append(entry)

        result = subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error("crontab update failed for watchdog probe {}: {}", role, result.stderr)
            return 1
        logger.info("crontab watchdog-probe entry added ({}, every {} min)", marker, minutes)
        return 0


def _unregister_linux(role: str, slug: str) -> int:
    with crontab_lock():
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return 0
        marker = _cron_marker(role, slug)
        lines = [line for line in result.stdout.splitlines() if marker not in line]
        if len(lines) == len(result.stdout.splitlines()):
            return 0
        subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        logger.info("crontab watchdog-probe entry removed ({})", marker)
    return 0


# Windows-only: how long ONE probe invocation may run before Task Scheduler ends
# it. Neither launchd nor cron bounds a job's runtime, so there is nothing to
# mirror — the bound exists because a Windows task's instance policy is
# `IgnoreNew`, so a wedged invocation blocks every later one until it is ended
# (with the scheduler's 72h default, three days of no supervision at all).
#
# 300s. The work is small — interpreter cold start, one pidfile read, one liveness
# check, and on dead ONE session respawn — and a respawn-and-verify of a daemon
# has been observed at 20-25s on the reference box, so this is roughly 10x the
# slowest legitimate run: enough headroom for a cold interpreter on a box whose
# antivirus is scanning the venv, while staying far under the hour that would make
# a wedge indistinguishable from an outage. The cost is bounded on the other side
# too: at a 60s cadence one invocation running to the bound skips at most four
# ticks, so a systematically wedging probe degrades revival latency from ~1min to
# ~5min rather than stopping.
_WINDOWS_TIME_LIMIT_S = 300


def _register_windows(role: str, interval_s: int) -> str | None:
    """Register this role's watchdog probe as a Windows scheduled task.

    Same contract as the launchd / crontab paths: idempotent, one task per
    capability, minute granularity (the interval is clamped by
    ``create_minute_task``)."""
    from shared.os_schtasks import create_minute_task

    return create_minute_task(
        f"watchdog-probe-{role}",
        ("cluster", "watchdog-probe", "--role", role),
        interval_s // 60,
        time_limit_s=_WINDOWS_TIME_LIMIT_S,
    )


def _unregister_windows(role: str, slug: str) -> int:
    from shared.os_schtasks import delete_task

    return delete_task(f"watchdog-probe-{role}", slug)


def register_watchdog_probe(
    role: MachineRole,
    interval_s: int = WATCHDOG_PROBE_INTERVAL_SECONDS,
) -> None:
    """Register the OS-scheduled watchdog probe for one capability.

    Platform-aware — delegates to ``PlatformBackend.register_watchdog_probe``.
    Idempotent: re-running updates the interval and reloads the job.
    A no-op when ``os_jobs_enabled()`` is off (the test suite).

    Raises:
        RuntimeError: on registration failure (POSIX). The Windows backend
        degrades to a loud warning instead — see `WindowsPlatformBackend`.
    """
    if not os_jobs_enabled():
        skip_os_job(f"watchdog-probe.{role}")
        return
    from shared.platform_backend import get_backend

    get_backend().register_watchdog_probe(role, interval_s=interval_s)


def unregister_watchdog_probe(role: MachineRole, home: Path | None = None) -> None:
    """Remove one capability's OS-scheduled watchdog probe.

    `home` selects WHICH cluster's job to remove; it defaults to this process's
    own home. See `shared.os_cron.unregister_os_cron` for why the target travels
    as an argument and not in `AVA_HOME`, and why `register_watchdog_probe` has
    no matching parameter.

    Platform-aware. Safe to call when no job is registered (no-op).
    """
    from shared.cluster import slug_for_home
    from shared.platform_backend import get_backend

    get_backend().unregister_watchdog_probe(role, slug_for_home(home))
