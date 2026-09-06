"""Boot-time autostart for a cluster's ava-managed services.

Registers an OS job that runs `ava start` on boot, so a machine reboot brings
the cluster's data plane + gateway / agents / daemons back up without a human
running `ava start` by hand. Each cluster owns its Postgres+Redis under its
`$AVA_HOME`, brought up by `ava start` itself (not a box-level brew/systemd
service), so this one boot job covers the whole cluster — data plane included.

- macOS: a launchd User LaunchAgent (RunAtLoad) in ~/Library/LaunchAgents/
- Linux: a user crontab `@reboot` entry
- Windows: a Task Scheduler `/SC ONLOGON` job (see shared/os_schtasks.py)

The job **retries** — one boot-time `ava start` is not enough, because at boot
its dependencies are not all up yet. See `shared/boot_policy.py` for the policy
and for why macOS expresses it with launchd keys while the other two run
`ava boot` (`cli/boot_retry.py`).

Mirrors shared/os_cron.py (the health-probe registrar) -- same launchd / crontab
/ schtasks mechanics -- but fires once at boot (RunAtLoad / @reboot / ONLOGON)
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
(`cli/commands/_cluster_instance.py` waits for the reachable address before
binding Postgres; PgBouncer and authenticated Linux Redis use the same wait).
macOS Redis remains loopback-only. That covers a LOCAL address only. An enrolled agent-runner's
start also depends on a REMOTE gateway — its Settings build fetches
`GET /api/bootstrap` (`shared.bootstrap`) — and on a VPN-joined runner that
interface comes up after the boot job fires. This module used to assert the
hazard class was already handled and emit a fire-once job; the retry above is
what actually covers it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from loguru import logger

from shared.boot_policy import BOOT_RETRY_INTERVAL_S
from shared.config import settings
from shared.os_cron import (
    LAUNCHD_LABEL_PREFIX,
    ava_binary_path,
    cleanup_legacy_macos_job,
    cron_env_prefix,
    launchd_env_block,
    os_jobs_enabled,
    skip_os_job,
)
from shared.platform import crontab_lock


def _home_slug() -> str:
    from shared.cluster import home_slug
    from shared.paths import ava_home

    return home_slug(ava_home())


def _legacy_tokens() -> list[str]:
    """This home's pre-path-only label tokens (see os_cron._legacy_label_tokens),
    used only to clean up old crontab/launchd entries."""
    from shared.os_cron import _legacy_label_tokens

    return _legacy_label_tokens()


def _autostart_label(slug: str) -> str:
    return f"{LAUNCHD_LABEL_PREFIX}.{slug}.autostart"


def _autostart_plist_path(slug: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_autostart_label(slug)}.plist"


def _autostart_plist_content() -> str:
    """The LaunchAgent plist: run `ava start` at load, and keep re-running it
    for as long as it fails.

    `KeepAlive` → `SuccessfulExit: false` is launchd's "restart in the inverse
    condition" of a zero exit (launchd.plist(5)) — i.e. respawn only while the
    job exits non-zero, and fall back to demand-based invocation the moment it
    exits 0. `ThrottleInterval` overrides launchd's default 10-second respawn
    floor, so a start that keeps failing is retried once a minute rather than
    six times a minute. Together that is the retry policy in
    `shared/boot_policy.py`, with launchd rather than a loop of ours doing the
    retrying — which is why this job runs plain `ava start` and not `ava boot`.

    `--no-readiness-gate` is exactly why that distinction needs handling here rather
    than only in `cli.boot_retry`. `SuccessfulExit` is a boolean: launchd cannot tell
    `ava start`'s "a step failed" (1, retry it) from "services launched, one is not
    serving yet" (`SERVICES_NOT_READY_EXIT_CODE`, do not retry forever). So the boot
    path opts out of the readiness verdict on every platform, and the three keep the
    single behaviour `boot_policy` insists they share. The flag suppresses only the
    exit code; the wait still happens and the unready services are still named in
    `autostart.log`.
    """
    label = _autostart_label(_home_slug())
    ava = ava_binary_path()
    log_file = Path(settings.general.ava_home) / "logs" / "autostart.log"
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
        <string>--no-readiness-gate</string>
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
    slug = _home_slug()
    plist_path = _autostart_plist_path(slug)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    content = _autostart_plist_content()
    if plist_path.exists() and plist_path.read_text() == content:
        return 0  # already current -- nothing to do, no reload
    plist_path.write_text(content)
    label = _autostart_label(slug)
    logger.info("Wrote autostart plist to {}", plist_path)
    print(  # noqa: T201
        f"  . '{label}' loads on next login/reboot; enable now with: "
        f"launchctl bootstrap gui/{os.getuid()} {plist_path}"
    )
    cleanup_legacy_macos_job("autostart")
    return 0


def _unregister_macos(slug: str) -> int:
    plist_path = _autostart_plist_path(slug)
    label = _autostart_label(slug)
    subprocess.run(  # noqa: S603
        ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"],
        capture_output=True,
        check=False,
    )
    if plist_path.exists():
        plist_path.unlink()
        logger.info("Removed autostart plist {}", plist_path)
    return 0


_CRON_MARKER = "# ava-autostart"


def _register_linux() -> int:
    """Add a `@reboot` crontab entry that runs `ava boot` for this cluster.

    `ava boot`, not `ava start`: a `@reboot` line fires exactly once and cron
    offers no retry, so the retry loop is ours (`cli/boot_retry.py`).

    A `@reboot` line only fires at boot, so writing it never triggers an
    immediate run (unlike macOS RunAtLoad) -- no recursion guard needed. When
    crontab is absent (a minimal box such as a hermetic bench / CI container),
    warn and skip rather than fail the whole bring-up, matching os_cron.
    """
    if shutil.which("crontab") is None:
        print(  # noqa: T201
            "  ! autostart: crontab not installed on this host (skipping); "
            "cluster will not auto-start on reboot"
        )
        return 0
    ava_path = ava_binary_path()
    slug = _home_slug()
    # `cron_env_prefix()` first (the assignment must precede the command for cron
    # to scope it to this line), then `boot` rather than `start` — the retry loop.
    entry = f"@reboot {cron_env_prefix()}{ava_path} boot  {_CRON_MARKER}.{slug}"

    with crontab_lock():
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if result.returncode != 0 and "no crontab" not in (result.stderr or "").lower():
            # Anything but the benign "no crontab for <user>" would make the rewrite
            # below clobber the user's real crontab from "".
            print(  # noqa: T201
                f"  * crontab -l failed ({result.stderr.strip() or result.returncode}); "
                "skipping autostart registration to avoid clobbering the crontab",
                file=sys.stderr,
            )
            return 1
        current = result.stdout if result.returncode == 0 else ""
        # Replace this home's entry (slug marker) AND its pre-path-only entries
        # (`--cluster <name>` markers, whose token was the old home-derived name).
        stale_markers = [f"{_CRON_MARKER}.{slug}"] + [
            f"{_CRON_MARKER}.{t}" for t in _legacy_tokens()
        ]
        lines = [line for line in current.splitlines() if not any(m in line for m in stale_markers)]
        lines.append(entry)

        result = subprocess.run(
            ["crontab", "-"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logger.error("crontab update failed: {}", result.stderr)
            return 1
        logger.info("crontab @reboot entry added ({}.{})", _CRON_MARKER, slug)
        return 0


def _unregister_linux(slug: str) -> int:
    with crontab_lock():
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return 0
        marker = f"{_CRON_MARKER}.{slug}"
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
        logger.info("crontab @reboot entry removed")
    return 0


def _register_windows() -> str | None:
    """Register cluster autostart as a Windows scheduled task.

    `/SC ONLOGON` — the user-session analog of launchd RunAtLoad and the Linux
    `@reboot` line. Unlike macOS, creating the task never runs it, so there is no
    recursion guard to worry about (see the module docstring).

    Runs `ava boot`, not `ava start`, for the same reason as Linux: an ONLOGON
    trigger cannot repeat. `schtasks /RI` — the only repetition knob the command
    line offers — is documented as "not applicable for schedule types: MINUTE,
    HOURLY, ONSTART, ONLOGON, ONIDLE, and ONEVENT", and wrapping the command in
    a `cmd.exe` retry loop would reintroduce the console flash `os_schtasks`
    picks `pythonw.exe` to avoid. So the loop is ours (`cli/boot_retry.py`).

    That loop is also why this is the one job registered with NO execution time
    limit. `ava boot` retries with no attempt cap deliberately (`boot_policy` —
    the three platforms must agree, and neither launchd nor cron bounds their
    equivalent's runtime), and nothing else recovers a host whose boot start never
    succeeded. Any finite limit would be a Windows-only attempt cap imposed by the
    scheduler on exactly that job; the default it replaces was a 72-hour one.

    A logon trigger only fires for an interactive logon, so a reboot nobody logs
    into leaves this cluster down — recorded as an operational limit in
    `conventions/windows-setup.md`."""
    from shared.os_schtasks import NO_TIME_LIMIT_S, create_logon_task

    return create_logon_task("autostart", ("boot",), time_limit_s=NO_TIME_LIMIT_S)


def _unregister_windows(slug: str) -> int:
    from shared.os_schtasks import delete_task

    return delete_task("autostart", slug)


def register_autostart() -> None:
    """Register the boot-time autostart job for this cluster.

    Platform-aware: delegates to ``PlatformBackend.register_autostart``.
    Idempotent. On macOS the plist is written but not loaded into the running
    session (see module docstring); it takes effect on the next reboot.
    A no-op when ``os_jobs_enabled()`` is off (the test suite).

    Raises:
        RuntimeError: on registration failure (POSIX). The Windows backend
        degrades to a loud warning instead — see `WindowsPlatformBackend`.
    """
    if not os_jobs_enabled():
        skip_os_job("autostart")
        return
    from shared.platform_backend import get_backend

    get_backend().register_autostart()


def unregister_autostart(home: Path | None = None) -> None:
    """Remove a cluster's boot-time autostart job. Safe when none is registered.

    `home` selects WHICH cluster's job to remove; it defaults to this process's
    own home. See `shared.os_cron.unregister_os_cron` for why the target travels
    as an argument and not in `AVA_HOME`, and why `register_autostart` has no
    matching parameter.
    """
    from shared.cluster import slug_for_home
    from shared.platform_backend import get_backend

    get_backend().unregister_autostart(slug_for_home(home))
