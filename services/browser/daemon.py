"""Shared headed Chrome launcher for the agent-runner.

Launches a real, headed Chrome with a remote-debugging (CDP) port and a
dedicated persistent profile. Same entry pattern as the other ava daemons
(`python -m services.<name>.daemon`); `os.execvp` makes the supervised session's
process become Chrome. Agents reach the browser over
localhost CDP through the chrome-devtools-mcp plugin (`--browserUrl`).

env overrides (via base.config.settings):
- `AVA_BROWSER_ENABLED` — gate (the ava-browser session only starts when true)
- `AVA_CHROME_BINARY` — explicit Chrome path; else the platform default

The CDP port is `settings.services.browser_cdp_port` (default 9222).

The profile lives at `$AVA_HOME/chrome-profile/` — dedicated and persistent,
separate from the user's daily Chrome profile (isolated cookie jar; the user
signs it in once). At first `ava start` the operator may instead seed it from
their daily Chrome (converge's `_ensure_browser` -> `profile.ensure_browser_profile`);
either way, by the time this daemon runs the profile dir is the source of truth
and `mkdir(exist_ok=True)` only backstops the fresh-empty case.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from loguru import logger

from base.config import settings
from base.host.system.probes import browser_incapability, resolve_chrome_binary
from base.paths import logs_dir

from . import macos_readiness
from . import profile as browser_profile
from .probe import cdp_url
from .profile import profile_dir as _profile_dir

_CDP_TIMEOUT_S = 2.0


def assert_browser_capable() -> None:
    """Raise RuntimeError with a precise, actionable message if this machine
    cannot host the shared headed browser. Called by the converge preflight and
    main(). The capability check itself lives in base.host.system.probes
    (browser_incapability); this raises its reason so a launch fails loudly with
    the same wording the operator sees in `ava status`."""
    reason = browser_incapability()
    if reason is not None:
        raise RuntimeError(f"ava-browser: {reason}")


def _cdp_reachable(port: int) -> bool:
    """True when a debuggable Chrome answers on this CDP port (`/json/version`
    -> 200). Before launch, a served port means a second Chrome on the same
    profile would collide on the singleton profile lock. main() refuses it.
    """
    url = cdp_url(port)
    try:
        with urllib.request.urlopen(url, timeout=_CDP_TIMEOUT_S) as resp:  # noqa: S310 — fixed localhost CDP URL
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _chrome_args(binary: str, port: int, profile: Path) -> list[str]:
    return [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def _launch(binary: str, args: list[str]) -> None:
    """Replace this process with Chrome so the session owns its native PID."""
    try:
        os.execvp(binary, args)  # noqa: S606 — intentional exec of the resolved Chrome binary
    except OSError as exc:
        logger.error("ava-browser exec failed: {}", exc)
        sys.exit(127)


def main() -> None:
    # Fail loud BEFORE the log redirect so the reason is visible in the
    # session log — this also guards healthcheck-triggered respawns, not just the first
    # launch (the converge preflight only runs at `ava start`).
    assert_browser_capable()
    cdp_port = settings.services.browser_cdp_port  # the recorded/`.env` port; default 9222
    if _cdp_reachable(cdp_port):
        # Something already serves this CDP port — either a manually-started
        # Chrome holding the profile lock, or one of ours that outlived its
        # session on a `SingletonLock` handoff. Launching a second Chrome on the
        # same --user-data-dir collides on that lock and exits at once, killing
        # this session. Refuse loudly: the browser service owns this port, so
        # the squatter has to go before `ava start` / the watchdog can run the
        # supervised one. The healthcheck does not respawn into this refusal
        # either — it pairs the CDP probe with a session-liveness probe, so a
        # squatter is reported (ERROR) rather than churned at.
        #
        # The message names its own remedy, because this refusal is what an
        # operator actually meets: `--stop-browser` now sweeps any Chrome on this
        # cluster's profile (services/browser/orphan.py), which clears the
        # post-handoff case without a manual pid hunt. A Chrome on some *other*
        # profile is deliberately not swept — it cannot be positively identified
        # as ours — so that case stays the operator's to quit.
        print(  # noqa: T201 — pre-redirect, surfaces in the session log
            f"ava-browser: CDP port {cdp_port} already served by another Chrome; "
            f"refusing to start a second instance on {_profile_dir()}. "
            "To clear it: `ava stop --stop-browser` then `ava start` — that sweeps any "
            "Chrome running on this cluster's profile, including one left behind by a "
            "SingletonLock handoff. If instead you started a Chrome of your own on this "
            "port, quit that one — the browser service owns this port.",
            file=sys.stderr,
        )
        sys.exit(1)
    binary = resolve_chrome_binary()
    if binary is None:  # assert_browser_capable already raised if so — this narrows the type
        sys.exit(127)
    # On macOS a detached process can see a display while lacking the active GUI
    # login session and Keychain context Chrome needs for encrypted profile data.
    # Wait in this supervised process instead of launching an unusable browser;
    # the matching marker lets the healthcheck report degraded state without
    # killing and respawning this deliberate wait.
    macos_readiness.wait_for_browser_startup_readiness()
    profile = _profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    local_state_warning = browser_profile.validate_local_state(profile)
    if local_state_warning is not None:
        logger.warning("ava-browser: Local State validation warning: {}", local_state_warning)
    # Redirect stdout/stderr to browser.log before execvp so a Chrome crash is
    # postmortem-able.
    log_fd = os.open(str(logs_dir() / "browser.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)
    _launch(binary, _chrome_args(binary, cdp_port, profile))


if __name__ == "__main__":
    main()
