"""Boot-time autostart for a cluster's ava-managed services.

Registers an OS job that runs `ava start` on boot, so a machine reboot brings
the cluster's data plane + gateway / agents / daemons back up without a human
running `ava start` by hand. Each cluster owns its Postgres+Redis under its
`$AVA_HOME`, brought up by `ava start` itself (not a box-level brew/systemd
service), so this one boot job covers the whole cluster — data plane included.

- macOS: a launchd User LaunchAgent (RunAtLoad) in ~/Library/LaunchAgents/
- Linux: the enabled distro-level systemd unit (`base.host.system.boot_unit`);
  automatic startup requires systemd

The job **retries** — one boot-time `ava start` is not enough, because at boot
its dependencies are not all up yet. See `base/host/system/boot_policy.py` for the policy
and for how each mechanism states it: launchd keys on macOS, systemd restart
keys on Linux.

Mirrors base/host/system/cron.py (the health-probe registrar) -- same launchd / crontab
mechanics -- but fires at boot (RunAtLoad / systemd)
instead of on an interval, and runs `ava start`.

Why the macOS path writes the plist but does NOT `launchctl bootstrap` it:
`bootstrap` on a RunAtLoad job runs it immediately, and this registrar is
invoked from converge, which runs *inside* `ava start` -- bootstrapping here
would spawn a second, concurrent `ava start`. launchd loads every plist in
~/Library/LaunchAgents/ at login, so dropping the file there is enough for it to
fire on the next boot (this box auto-logs-in). Enabling it in the current
session is a one-line manual step printed below, not something converge does.

Only ONE boot-ordering hazard is handled inside `ava start` itself: a routable
bind address not up yet when this cluster's Postgres, PgBouncer or Linux Redis starts
(`cli/commands/data_plane/cluster_instance.py` waits for the reachable address before
binding Postgres; PgBouncer and authenticated Linux Redis use the same wait).
macOS Redis remains loopback-only. That covers a LOCAL address only. An enrolled agent-runner's
start also depends on a REMOTE gateway — its Settings build fetches
`GET /api/bootstrap` (`base.host.env.bootstrap`) — and on a VPN-joined runner that
interface comes up after the boot job fires. This module used to assert the
hazard class was already handled and emit a fire-once job; the retry above is
what actually covers it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from loguru import logger

from base.host.system.boot_policy import BOOT_RETRY_INTERVAL_S
from base.host.system.cron import (
    LAUNCHD_LABEL_PREFIX,
    ava_binary_path,
    launchd_env_block,
    os_jobs_enabled,
    owns_os_jobs,
    skip_os_job,
)
from base.paths import ava_home

_LABEL = f"{LAUNCHD_LABEL_PREFIX}.autostart"


def _autostart_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _autostart_plist_content() -> str:
    """The LaunchAgent plist: run `ava start` at load, and keep re-running it
    for as long as it fails.

    `KeepAlive` → `SuccessfulExit: false` is launchd's "restart in the inverse
    condition" of a zero exit (launchd.plist(5)) — i.e. respawn only while the
    job exits non-zero, and fall back to demand-based invocation the moment it
    exits 0. `ThrottleInterval` overrides launchd's default 10-second respawn
    floor, so a start that keeps failing is retried once a minute rather than
    six times a minute. Together that is the retry policy in
    `base/host/system/boot_policy.py`, with launchd rather than a loop of ours doing the
    retrying — which is why this job runs plain `ava start` and not `ava boot`.

    Start remains unsuccessful until readiness passes. Its idempotent root
    reconciliation preserves healthy units on subsequent boot attempts.
    """
    label = _LABEL
    ava = ava_binary_path()
    log_file = ava_home() / "logs" / "autostart.log"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{ava}</string>
        <string>start</string>
    </array>
{launchd_env_block()}
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>{BOOT_RETRY_INTERVAL_S}</integer>
    <key>StandardOutPath</key>
    <string>{log_file}</string>
    <key>StandardErrorPath</key>
    <string>{log_file}</string>
</dict>
</plist>
"""


def _register_macos() -> int:
    """Write (or update) the autostart LaunchAgent plist. Idempotent: rewrites
    only when the content changed, and never bootstraps (see module docstring)."""
    plist_path = _autostart_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    content = _autostart_plist_content()
    if plist_path.exists() and plist_path.read_text() == content:
        return 0  # already current -- nothing to do, no reload
    plist_path.write_text(content)
    label = _LABEL
    logger.info("Wrote autostart plist to {}", plist_path)
    print(  # noqa: T201
        f"  . '{label}' loads on next login/reboot; enable now with: "
        f"launchctl bootstrap gui/{os.getuid()} {plist_path}"
    )
    return 0


def _unregister_macos() -> int:
    plist_path = _autostart_plist_path()
    label = _LABEL
    subprocess.run(  # noqa: S603
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
    )
    if plist_path.exists():
        plist_path.unlink()
        logger.info("Removed autostart plist {}", plist_path)
    return 0


def register_autostart() -> None:
    """Register the host's boot-time autostart job.

    Platform-aware: delegates to ``PlatformBackend.register_autostart``.
    Idempotent. On macOS the plist is written but not loaded into the running
    session (see module docstring); it takes effect on the next reboot.
    A no-op when ``os_jobs_enabled()`` is off (the test suite) or when this
    process's home is not the default home (``owns_os_jobs``).

    Raises:
        RuntimeError: on registration failure.
    """
    if not os_jobs_enabled():
        skip_os_job("autostart")
        return
    if not owns_os_jobs("autostart"):
        return
    from base.host.system.backend import get_backend

    get_backend().register_autostart()


def unregister_autostart() -> None:
    """Remove the host's boot job (systemd / launchd).

    Safe when none is registered, and a no-op when this process's home is not the
    default home (``owns_os_jobs``).
    """
    if not owns_os_jobs("autostart"):
        return
    from base.host.system.backend import get_backend

    get_backend().unregister_autostart()
