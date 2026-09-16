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

ONE exception to the dumb rule (2026-09-17 wave-2 abort): a maintenance stop
kills the watchdog on purpose, and its services phase refuses to certify while
anything reappears — so a probe tick landing inside that window would revive
the watchdog and abort the stop. While a fresh ``held-stop`` marker says a
stop holds this home, the probe skips revival entirely; see `HeldStopState`
below.

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
import time
from enum import StrEnum
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


_LAUNCHCTL_TIMEOUT_S = 5.0
_BOOTOUT_SETTLE_S = 10.0


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=_LAUNCHCTL_TIMEOUT_S,
    )


def _job_loaded(service: str) -> bool:
    result = _launchctl("print", service)
    if result.returncode == 0:
        return True
    if result.returncode in (3, 113):  # ESRCH / launchctl's missing-service verdict
        return False
    raise RuntimeError(f"cannot inspect launchd job {service}: {result.stderr.strip()}")


def _unload_before_bootstrap(service: str) -> None:
    result = _launchctl("bootout", service)
    if result.returncode not in (0, 3, 113):
        raise RuntimeError(f"cannot unload launchd job {service}: {result.stderr.strip()}")
    # bootout can return before launchd removes the service. Reusing its label
    # in that window produces bootstrap EIO even for a valid plist.
    deadline = time.monotonic() + _BOOTOUT_SETTLE_S
    while _job_loaded(service):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"launchd job {service} did not unload within {_BOOTOUT_SETTLE_S}s")
        time.sleep(0.1)


def _register_macos(role: str, interval_s: int) -> int:
    """Write + load the LaunchAgent for ``role``'s watchdog probe. Idempotent."""
    slug = _home_slug()
    plist_path = _plist_path(role, slug)
    label = probe_label(role, slug)
    service = f"gui/{os.getuid()}/{label}"
    content = _plist_content(role, interval_s)
    from shared.platform import launchd_job_label

    if launchd_job_label() == label:
        # Unloading our ancestor would kill this converge. Keep the old spec
        # intact so an external converge can still detect the pending change.
        logger.info("Watchdog probe '{}' is registering itself — deferring reload", label)
        return 0
    loaded = _job_loaded(service)
    if loaded and plist_path.exists() and plist_path.read_text() == content:
        return 0
    if loaded:
        _unload_before_bootstrap(service)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    # Publish the desired file only after removal is confirmed. An unsuccessful
    # unload must not leave a new file falsely certifying the old loaded job.
    plist_path.write_text(content)
    result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path))
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


# --- held-stop marker (the stop side and the probe side) --------------------


# A maintenance stop kills the watchdog on purpose (it is signalled first, see
# `_maintenance_stop.stop_services`), and the stop's services phase refuses to
# certify while any service reappears ("services appeared during held stop").
# The probe above is deliberately gate-blind — dead, respawn — so a probe tick
# landing inside the stop window revives the watchdog and aborts the stop
# (2026-09-17 wave-2: a 120.9s services phase crossed a 60s probe tick). The
# marker below is the one piece of shared state that closes the race:
# `stop_services` publishes it before the first signal and clears it in a
# `finally`; the probe reads it and skips revival while it is fresh.
#
# Scope: session mode only. A root-driven host (`services.root_driver_enabled`)
# runs no probe at all — converge retires it and the root supervisor's
# HealthMonitor absorbs the watchdogs — so there is nothing to suppress there.
#
# Marker: ``$AVA_HOME/state/held-stop``, one line ``<unix-ts> <pid> <home>``.
# The pid and home are operator diagnostics; the reader keys on the timestamp.
# TTL 30 min — a stop that dies without its `finally` (SIGKILL, OOM) must not
# suppress revival forever; a healthy stop clears the marker on every path.

HELD_STOP_TTL_S = 30 * 60.0

_HELD_STOP_NAME = "held-stop"


class HeldStopState(StrEnum):
    """Classification of this home's held-stop marker.

    FRESH suppresses probe revival; STALE / ABSENT / UNREADABLE are the probe's
    normal dumb job — revive a dead watchdog. Unreadable fails OPEN on purpose:
    a broken marker must not disable supervision, because an unsupervised
    capability outlives the race the marker guards against.
    """

    FRESH = "fresh"
    STALE = "stale"
    ABSENT = "absent"
    UNREADABLE = "unreadable"


def held_stop_marker_path() -> Path:
    """This home's held-stop marker path — resolved per call, never cached.

    ``$AVA_HOME/state/held-stop``: the state dir already holds per-home runtime
    state, and per-home scope keeps a co-located second cluster's stop from
    touching this one's marker.
    """
    return Path(settings.general.ava_home) / "state" / _HELD_STOP_NAME


def held_stop_state(*, now: float | None = None) -> HeldStopState:
    """Classify this home's held-stop marker; never raises.

    ``now`` overrides the clock (tests). A timestamp in the future (clock step)
    reads FRESH: when the timestamp cannot be trusted, suppression during a
    possible stop is the safe side. Its horizon is the clock skew plus the TTL
    (the clock must catch up before the age can pass the TTL) — bounded, not
    TTL-tight.
    """
    try:
        written = float(held_stop_marker_path().read_text().split()[0])
    except FileNotFoundError:
        return HeldStopState.ABSENT
    except (OSError, IndexError, ValueError):
        return HeldStopState.UNREADABLE
    if (time.time() if now is None else now) - written > HELD_STOP_TTL_S:
        return HeldStopState.STALE
    return HeldStopState.FRESH


def write_held_stop_marker() -> None:
    """Publish this home's held-stop window (called by `stop_services`).

    Content: ``<unix-ts> <pid> <home>`` — the reader keys on the timestamp; the
    pid and home are operator diagnostics for "which stop wrote this". Best
    effort: an unwritable state dir is logged, not raised — the marker is
    advisory coordination and a failed write must not fail the stop it protects
    (the stop degrades to pre-marker behavior; only the probe-tick race
    returns).
    """
    try:
        path = held_stop_marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{time.time():.3f} {os.getpid()} {settings.general.ava_home}\n")
    except OSError as exc:
        logger.warning(
            "cannot publish held-stop marker ({}); a probe tick may revive a watchdog mid-stop",
            exc,
        )


def clear_held_stop_marker() -> None:
    """Remove the marker; idempotent, never raises.

    Called from `stop_services`'s ``finally`` on every outcome, so it must not
    raise over the stop's own error — but a marker that survives (permissions)
    keeps the probe suppressed until the TTL, which is worth a log line.
    """
    try:
        held_stop_marker_path().unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "cannot clear held-stop marker ({}); probe revival stays suppressed until its TTL",
            exc,
        )
