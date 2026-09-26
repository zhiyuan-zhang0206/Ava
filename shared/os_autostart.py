"""Boot-time autostart for a cluster's ava-managed services.

Registers an OS job that runs `ava start` on boot, so a machine reboot brings
the cluster's data plane + gateway / agents / daemons back up without a human
running `ava start` by hand. Each cluster owns its Postgres+Redis under its
`$AVA_HOME`, brought up by `ava start` itself (not a box-level brew/systemd
service), so this one boot job covers the whole cluster — data plane included.

- macOS: a launchd User LaunchAgent (RunAtLoad) in ~/Library/LaunchAgents/
- Linux: the enabled distro-level systemd unit (`shared.os_boot_unit`);
  automatic startup requires systemd
- Windows: a Task Scheduler `/SC ONLOGON` job (see shared/os_schtasks.py)

The job **retries** — one boot-time `ava start` is not enough, because at boot
its dependencies are not all up yet. See `shared/boot_policy.py` for the policy
and for how each mechanism states it: launchd keys on macOS, systemd restart
keys on Linux, `ava boot` (`cli/boot_retry.py`) on Windows.

Mirrors shared/os_cron.py (the health-probe registrar) -- same launchd / crontab
/ schtasks mechanics -- but fires at boot (RunAtLoad / systemd / ONLOGON)
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
import subprocess
from pathlib import Path

from loguru import logger

from shared.boot_policy import BOOT_RETRY_INTERVAL_S
from shared.config import settings
from shared.os_cron import (
    LAUNCHD_LABEL_PREFIX,
    ava_binary_path,
    launchd_env_block,
    os_jobs_enabled,
    skip_os_job,
)
from shared.platform import IS_MACOS


def _home_slug() -> str:
    from shared.cluster import home_slug
    from shared.paths import ava_home

    return home_slug(ava_home())


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

    Start remains unsuccessful until readiness passes. Its idempotent root
    reconciliation preserves healthy units on subsequent boot attempts.
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


def _register_windows() -> str | None:
    """Register cluster autostart as a Windows scheduled task.

    `/SC ONLOGON` — the user-session analog of launchd RunAtLoad and Linux boot target. Unlike macOS, creating the task never runs it, so there is no
    recursion guard to worry about (see the module docstring).

    Runs `ava boot`, not `ava start`, because an ONLOGON
    trigger cannot repeat. `schtasks /RI` — the only repetition knob the command
    line offers — is documented as "not applicable for schedule types: MINUTE,
    HOURLY, ONSTART, ONLOGON, ONIDLE, and ONEVENT", and wrapping the command in
    a `cmd.exe` retry loop would reintroduce the console flash `os_schtasks`
    picks `pythonw.exe` to avoid. So the loop is ours (`cli/boot_retry.py`).

    That loop is also why this is the one job registered with NO execution time
    limit. `ava boot` retries with no attempt cap deliberately (`boot_policy` —
    the three platforms must agree, and neither launchd nor systemd bounds their
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
    """Remove this exact home's boot job (systemd / launchd / task).

    Safe when none is registered.

    `home` selects WHICH cluster's job to remove; it defaults to this process's
    own home. See `shared.os_cron.unregister_os_cron` for why the target travels
    as an argument and not in `AVA_HOME`, and why `register_autostart` has no
    matching parameter.
    """
    from shared.paths import ava_home
    from shared.platform_backend import get_backend

    get_backend().unregister_autostart(home if home is not None else ava_home())


def gui_domain_kickstart_command() -> str:
    """The shell line that (re)runs this cluster's autostart job in the current
    user's GUI domain, safe on a domain that has never loaded it.

    The remedy operator-facing messages print (task #3346): ensure the GUI
    domain knows the job — a fresh registration is only loaded at the next
    login, so a bare kickstart would fail with "Could not find service" — then
    kickstart it. One copy-pasteable line, the same two steps
    `relaunch_via_gui_domain` performs."""
    slug = _home_slug()
    label = _autostart_label(slug)
    domain = f"gui/{os.getuid()}"
    plist_path = _autostart_plist_path(slug)
    return (
        f"(launchctl print {domain}/{label} >/dev/null 2>&1 "
        f"|| launchctl bootstrap {domain} {plist_path}) "
        f"&& launchctl kickstart -k {domain}/{label}"
    )


def _ensure_macos_job_loaded(label: str, plist_path: Path, domain: str) -> str | None:
    """Load this cluster's autostart job into `domain` if it is not there yet.

    None on success (already loaded, or just bootstrapped), else why it could
    not be ensured. The shared prelude of the two kickstart entry points; the
    caller owns what it then does with the job."""
    if not plist_path.exists():
        return f"no autostart plist at {plist_path} (registered on the next `ava start`)"
    loaded = subprocess.run(  # noqa: S603
        ["launchctl", "print", f"{domain}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if loaded.returncode != 0:
        bootstrapped = subprocess.run(  # noqa: S603
            ["launchctl", "bootstrap", domain, str(plist_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if bootstrapped.returncode != 0:
            error = bootstrapped.stderr.strip() or f"exit {bootstrapped.returncode}"
            return f"launchctl bootstrap {label} failed: {error}"
    return None


def relaunch_via_gui_domain() -> tuple[bool, str]:
    """Run this cluster's autostart job in the GUI login session, now.

    The remedy for a service chain that lost the GUI login session context: a
    daemon respawned from an agent/SSH chain inherits launchd's "Background"
    management domain, where securityd denies every login-Keychain query, so a
    service that needs the Keychain (the headed browser, via its readiness
    gate) can never recover from there. Only launchd can cross domains, so this
    ensures the cluster's autostart LaunchAgent is loaded in ``gui/<uid>`` and
    kickstarts it — the job's `ava start` then runs under the GUI session and
    rebuilds the affected sessions.

    Writes nothing to disk: the plist is registered by converge. This only
    loads it when the GUI domain does not have it yet (the manual step
    ``_register_macos`` prints at registration) and starts it. macOS only.

    The caller owns the surrounding discipline (stopping the stuck session so
    `ava start` does not skip it, and bounding repeated kicks).

    Returns:
        ``(ok, detail)`` — ``detail`` names the job and domain on success, or
        why the relaunch could not be started.
    """
    if not IS_MACOS:
        return False, "the GUI domain only exists on macOS"
    slug = _home_slug()
    label = _autostart_label(slug)
    plist_path = _autostart_plist_path(slug)
    domain = f"gui/{os.getuid()}"
    error = _ensure_macos_job_loaded(label, plist_path, domain)
    if error is not None:
        return False, error
    kicked = subprocess.run(  # noqa: S603
        ["launchctl", "kickstart", "-k", f"{domain}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if kicked.returncode != 0:
        error = kicked.stderr.strip() or f"exit {kicked.returncode}"
        return False, f"launchctl kickstart {label} failed: {error}"
    return True, f"{label} relaunched in {domain}"


def ensure_via_gui_domain() -> tuple[bool, str]:
    """Run this cluster's autostart job in the GUI login session, without killing
    a running instance.

    The handover sibling of `relaunch_via_gui_domain` (task #3348): an `ava
    start` that must not bring services up in its own wrong launchd domain asks
    launchd to run the job now. `kickstart -p` (no ``-k``) never kills an
    already-running instance — measured: the same pid comes back and no second
    process exists (``-k`` is the kill-first variant the heal uses) — and ``-p``
    reports the running pid either way. macOS only; nothing is written to disk.

    Returns:
        ``(ok, detail)`` — ``detail`` names the job, domain and pid on success,
        or why the job could not be ensured.
    """
    if not IS_MACOS:
        return False, "the GUI domain only exists on macOS"
    slug = _home_slug()
    label = _autostart_label(slug)
    plist_path = _autostart_plist_path(slug)
    domain = f"gui/{os.getuid()}"
    error = _ensure_macos_job_loaded(label, plist_path, domain)
    if error is not None:
        return False, error
    kicked = subprocess.run(  # noqa: S603
        ["launchctl", "kickstart", "-p", f"{domain}/{label}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if kicked.returncode != 0:
        error = kicked.stderr.strip() or f"exit {kicked.returncode}"
        return False, f"launchctl kickstart {label} failed: {error}"
    pid = kicked.stdout.strip()
    return True, f"{label} running in {domain}" + (f" (pid {pid})" if pid else "")
