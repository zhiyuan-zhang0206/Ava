"""Read-only macOS startup readiness for the shared headed Chrome.

Chrome obtains the profile encryption key from the logged-in user's Keychain.
The detached browser service has no usable browser until both that GUI session
and its login Keychain are ready, so starting it over SSH can otherwise create
an unusable profile process. This module only observes those prerequisites; it
never unlocks a Keychain, changes the login session, or touches Chrome data.

While readiness is absent the daemon waits and logs the reason. The browser
healthcheck reads the same prerequisites with a direct probe
(``degraded_wait_reason``) to name the wait in its verdict; it records no
state, and no probe relaunches the service. One wait is diagnosed as
unfixable in place: ``launchctl managername`` says whether the process chain
runs inside the GUI login session ("Aqua") or outside it ("Background" — an
agent or SSH chain, or a respawn from one), where no amount of waiting makes
the login Keychain readable. The reason text names that case; an operator
must relaunch the service from the GUI domain.
"""

from __future__ import annotations

import logging
import os
import pwd
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import base.host.proc
from base.native_process.os_platform import is_macos

_log = logging.getLogger("services.desktop.browser.macos_readiness")

_PROBE_TIMEOUT_S = 5.0
READINESS_RETRY_S = 5.0
_READINESS_REPORT_INTERVAL_S = 300.0


@dataclass(frozen=True)
class StartupReadiness:
    """Whether it is safe to launch headed Chrome now, plus a safe reason if not."""

    ready: bool
    reason: str | None = None


@dataclass(frozen=True)
class ProbeResult:
    """Normalized bounded system-query result, including a timeout as non-zero."""

    returncode: int
    stdout: str
    stderr: str


def run_probe(argv: list[str]) -> ProbeResult:
    """Run a fixed read-only system query without letting a GUI prompt hang us."""
    try:
        completed = base.host.proc.run_bounded(
            argv, timeout=_PROBE_TIMEOUT_S, capture_output=True, text=True
        )
    except FileNotFoundError:
        return ProbeResult(127, "", f"{argv[0]} is unavailable")
    except subprocess.TimeoutExpired:
        return ProbeResult(124, "", f"timed out after {_PROBE_TIMEOUT_S:.0f}s")
    return ProbeResult(
        completed.returncode,
        str(completed.stdout or ""),
        str(completed.stderr or ""),
    )


def current_account() -> tuple[str, Path]:
    """Current process account and its home directory on macOS.

    This is intentionally account-derived rather than inherited from shell
    environment variables, which may describe the SSH caller rather than the
    launch context of the detached service.
    """
    entry = pwd.getpwuid(os.getuid())
    return entry.pw_name, Path(entry.pw_dir)


def probe_startup_readiness() -> StartupReadiness:
    """Check macOS GUI, launch-domain and Keychain prerequisites without changing
    either.

    `/dev/console` names the user physically attached to the GUI session. The
    matching `launchctl gui/<uid>` namespace verifies that current account has
    a GUI login context, `launchctl managername` verifies THIS process chain
    runs in it ("Aqua"), and `security show-keychain-info` is the smallest
    Keychain operation that proves the login Keychain can be queried. A failure
    is deliberately a wait condition, never an unlock attempt.
    """
    if not is_macos():
        return StartupReadiness(ready=True)

    account, home = current_account()
    console = run_probe(["/usr/bin/stat", "-f%Su", "/dev/console"])
    console_user = console.stdout.strip()
    if console.returncode != 0 or console_user != account:
        actual = console_user or "none"
        return StartupReadiness(
            ready=False,
            reason=f"no active GUI login session for {account} (console user is {actual})",
        )

    gui = run_probe(["/bin/launchctl", "print", f"gui/{os.getuid()}"])
    if gui.returncode != 0:
        return StartupReadiness(
            ready=False,
            reason=f"no active GUI login session for {account} (launchctl gui/{os.getuid()} unavailable)",
        )

    # A GUI session existing for this account is not the same as THIS chain
    # running in it: launchd inherits the management domain down the process
    # chain, and a daemon respawned from an agent or SSH chain lands in
    # "Background", where securityd denies every login-Keychain query ("User
    # interaction is not allowed") no matter how unlocked and ready the
    # Keychain is. Report that distinctly — the Keychain probe below can only
    # blame the Keychain, and waiting never fixes a wrong domain. An empty or
    # failed answer is not evidence either way and falls through.
    domain = run_probe(["/bin/launchctl", "managername"])
    managed = domain.stdout.strip()
    if domain.returncode == 0 and managed and managed != "Aqua":
        return StartupReadiness(
            ready=False,
            reason=(
                f"this process is not in the GUI login session "
                f"(launchd managername: {managed}); the login Keychain is unreachable "
                f"from this process chain and a relaunch from the GUI domain is required"
            ),
        )

    keychain = home / "Library" / "Keychains" / "login.keychain-db"
    keychain_probe = run_probe(["/usr/bin/security", "show-keychain-info", str(keychain)])
    if keychain_probe.returncode != 0:
        detail = keychain_probe.stderr.strip().splitlines()[-1:] or ["not available"]
        return StartupReadiness(ready=False, reason=f"login Keychain is not ready ({detail[0]})")
    return StartupReadiness(ready=True)


def degraded_wait_reason() -> str | None:
    """Why the browser's startup readiness is currently degraded, or None.

    A direct, bounded, read-only probe — the same one the daemon's wait loop
    runs. An unexpected macOS probe failure fails safe to degraded rather than
    launching Chrome without an answer about its prerequisites.
    """
    try:
        readiness = probe_startup_readiness()
    except Exception:  # Healthchecks must not turn an unknown readiness state into a crash loop.
        _log.exception("meiva-browser: startup readiness probe failed")
        if is_macos():
            return "macOS startup readiness could not be probed"
        return None
    if readiness.ready:
        return None
    return readiness.reason or "macOS browser startup readiness is unavailable"


def wait_for_browser_startup_readiness() -> None:
    """Block the supervised daemon until macOS can safely serve Chrome's keys."""
    last_reason: str | None = None
    last_reported_at = 0.0
    # quiesce-exempt: a bounded startup wait on browser readiness; no database
    while True:
        readiness = probe_startup_readiness()
        if readiness.ready:
            return
        reason = readiness.reason or "macOS browser startup readiness is unavailable"
        now = time.monotonic()
        if reason != last_reason or now - last_reported_at >= _READINESS_REPORT_INTERVAL_S:
            _log.warning(
                "meiva-browser DEGRADED: %s; waiting before Chrome launch and retrying every %.0fs",
                reason,
                READINESS_RETRY_S,
            )
            last_reason = reason
            last_reported_at = now
        time.sleep(READINESS_RETRY_S)
