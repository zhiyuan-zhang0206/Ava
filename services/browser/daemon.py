"""Shared headed Chrome launcher for the agent-runner.

Launches a real, headed Chrome with a remote-debugging (CDP) port and a
dedicated persistent profile. Same entry pattern as the other ava daemons
(`python -m services.<name>.daemon`); the supervised session owns the Chrome
process — by `os.execvp` on POSIX (the pane's process becomes Chrome) and, on
Windows which has no exec, by staying its parent and holding the session open
for as long as the *browser* lives (see `_launch` / `_supervise_chrome`). That
last distinction matters: Chrome's launched process is not always the process
that stays alive, so the Windows supervisor treats CDP reachability — not its
child's exit — as the browser-liveness fact. Agents reach the browser over
localhost CDP through the chrome-devtools-mcp plugin (`--browserUrl`).

env overrides (via shared.config.settings):
- `AVA_BROWSER_ENABLED` — gate (the ava-browser session only starts when true)
- `AVA_CHROME_BINARY` — explicit Chrome path; else the platform default
- `AVA_APP_PORT` — the Next.js app port; when this host serves the frontend
  (the value is set), Chrome opens it as its first tab

The CDP port is fixed at 9222 (`_CDP_PORT`).

The profile lives at `$AVA_HOME/chrome-profile/` — dedicated and persistent,
separate from the user's daily Chrome profile (isolated cookie jar; the user
signs it in once). At first `ava start` the operator may instead seed it from
their daily Chrome (converge's `_ensure_browser` -> `profile.ensure_browser_profile`);
either way, by the time this daemon runs the profile dir is the source of truth
and `mkdir(exist_ok=True)` only backstops the fresh-empty case.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger

from shared.config import settings
from shared.paths import logs_dir
from shared.platform import IS_WINDOWS
from shared.platform_probes import browser_incapability, resolve_chrome_binary

from .probe import cdp_url
from .profile import profile_dir as _profile_dir

_CDP_PORT = settings.services.browser_cdp_port  # per-cluster (cluster port block); default 9222
_CDP_TIMEOUT_S = 2.0

# Bounds on `_cdp_confirmed_gone` — the "is Chrome really gone?" question the
# Windows supervisor asks once its child exits. Deliberately finite: an
# unreachable CDP endpoint must produce an exit, not a retry loop.
_CDP_CONFIRM_ATTEMPTS = 6
_CDP_CONFIRM_INTERVAL_S = 0.5
_CDP_CONFIRM_DEADLINE_S = 10.0
# Cadence of the post-handoff watch (`_watch_cdp`) — this replaces `proc.wait()`
# as the thing that blocks until the browser is gone.
_CDP_WATCH_INTERVAL_S = 5.0


def assert_browser_capable() -> None:
    """Raise RuntimeError with a precise, actionable message if this machine
    cannot host the shared headed browser. Called by the converge preflight and
    main(). The capability check itself lives in shared.platform_probes
    (browser_incapability); this raises its reason so a launch fails loudly with
    the same wording the operator sees in `ava status`."""
    reason = browser_incapability()
    if reason is not None:
        raise RuntimeError(f"ava-browser: {reason}")


def _cdp_reachable(port: int) -> bool:
    """True when a debuggable Chrome answers on this CDP port (`/json/version`
    -> 200). One probe, two readings depending on when it is asked:

    - **before launch** — the port is already served, so a second Chrome on the
      same profile would collide on the singleton profile lock and
      forward-then-exit, leaving a dead session. main() refuses instead of
      launching into that collision.
    - **after the launched process exits** — Chrome is still up, so the exit was
      a handoff and not a death (see `_cdp_confirmed_gone`).
    """
    url = cdp_url(port)
    try:
        with urllib.request.urlopen(url, timeout=_CDP_TIMEOUT_S) as resp:  # noqa: S310 — fixed localhost CDP URL
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _cdp_confirmed_gone(port: int) -> bool:
    """True only when CDP stays unreachable for the whole confirmation window.

    The process Chrome's binary starts is not always the process that stays
    alive: on a `SingletonLock` handoff the binary forwards its command line to
    an existing (or newly detached) browser and exits promptly with success while
    Chrome runs on. "The process I spawned exited" and "Chrome is gone" are two
    different facts, so the supervisor asks the second one directly rather than
    inferring it from the first.

    Bounded on purpose — fail fast, no unbounded retry: at most
    `_CDP_CONFIRM_ATTEMPTS` probes, and it stops issuing new ones once
    `_CDP_CONFIRM_DEADLINE_S` of wall clock has passed, whichever comes first
    (worst case ~one extra `_CDP_TIMEOUT_S` for the probe already in flight). The
    window has to outlast a detached Chrome rebinding the port on a loaded box
    while staying far inside the 60s healthcheck cadence, so a genuinely dead
    browser is still respawned within one round. A single reachable answer ends
    it early: Chrome is alive.
    """
    deadline = time.monotonic() + _CDP_CONFIRM_DEADLINE_S
    for attempt in range(_CDP_CONFIRM_ATTEMPTS):
        if _cdp_reachable(port):
            return False
        if attempt + 1 == _CDP_CONFIRM_ATTEMPTS or time.monotonic() >= deadline:
            break
        time.sleep(_CDP_CONFIRM_INTERVAL_S)
    return True


def _watch_cdp(port: int) -> None:
    """Block until Chrome is confirmed gone, then return.

    The post-handoff stand-in for `proc.wait()`: once Chrome has moved out of
    this process tree, CDP reachability is the only liveness signal left, so the
    supervisor polls it. Unbounded in duration by design — exactly as
    `proc.wait()` is — but it has one exit and that exit is "CDP unreachable",
    each round confirmed through the bounded `_cdp_confirmed_gone`.
    """
    while True:
        time.sleep(_CDP_WATCH_INTERVAL_S)
        if _cdp_confirmed_gone(port):
            return


def _frontend_url() -> str | None:
    """The Ava frontend URL to open as the browser's first tab, or None when
    this host serves none locally.

    The tab is the Next.js app port (AVA_APP_PORT), not the gate's entry port:
    the entry is the always-up static gate, while the app port is the frontend
    itself. The port is only set by converge on gateway-capable hosts, so an
    agent-runner-only unit (no local frontend) gets no first tab.

    The host comes from AVA_GATEWAY_URL (the cluster's private-network
    address, never localhost) — same derivation as
    shared.alerts.frontend_base_url; a missing gateway URL means no first tab
    rather than a dead loopback URL.
    """
    port = settings.services.app_port
    if port is None:
        return None
    host = urlsplit(settings.gateway.gateway_url or "").hostname
    if not host:
        return None
    return f"http://{host}:{port}"


def _chrome_args(
    binary: str, port: int, profile: Path, first_tab_url: str | None = None
) -> list[str]:
    args = [
        binary,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if first_tab_url is not None:
        # A positional non-flag argument is a URL Chrome opens as a tab —
        # the user's entry point to the fleet UI on a fresh browser.
        args.append(first_tab_url)
    return args


def _supervise_chrome(args: list[str], port: int) -> int:
    """Windows half of `_launch`: stay Chrome's parent, and return the exit code
    that reports Chrome's fate. Non-zero whenever the browser is gone, so the
    session dies and the healthcheck/watchdog respawns it.

    The child exiting is *not* the fact this needs. Chrome's launched process is
    not always the process that stays alive — a `SingletonLock` handoff makes the
    binary forward its command line to another browser process and exit with
    success while Chrome runs on (observed on the `win` runner 2026-07-28).
    Waiting on the child and exiting with its status therefore killed the session
    under a perfectly healthy browser, which the supervisor read as a death:
    spurious respawn, and — because the surviving Chrome still holds the CDP port
    — a respawn the port guard then refuses forever. So a child exit only opens
    the question, and `_cdp_confirmed_gone` answers it:

    - **CDP unreachable** — Chrome really is gone. Exit non-zero for a respawn.
    - **CDP reachable** — handoff. Chrome outlived its launcher, so keep
      supervising via `_watch_cdp` and exit only once CDP goes away.

    `stdout`/`stderr` are pinned to fds 1/2 explicitly rather than left to
    inherit: main() has already `dup2`-ed them onto browser.log, but that moves
    the CRT fds only — Windows' `GetStdHandle` still points at what winproc
    redirected the launcher to, so an inheriting child would miss the postmortem
    log this daemon exists to keep.
    """
    try:
        proc = subprocess.Popen(  # noqa: S603 — resolved Chrome binary + our own argv
            args, stdout=1, stderr=2, close_fds=True
        )
    except OSError as exc:
        logger.error("ava-browser launch failed: {}", exc)
        return 127
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        # Ctrl-Break from `ava stop` lands on this process first. Take Chrome
        # down with us rather than orphaning it back onto the CDP port. A
        # deliberate stop needs no CDP confirmation — we are the ones leaving.
        proc.terminate()
        return proc.wait()
    if _cdp_confirmed_gone(port):
        logger.info(
            "ava-browser: launcher exited {} and CDP :{} is unreachable — Chrome is gone",
            code,
            port,
        )
        # A clean-looking 0 still means "this host has no browser", and this
        # service is meant to stay up: exit non-zero so the supervisor respawns.
        return code or 1
    logger.warning(
        "ava-browser: launcher exited {} but CDP :{} still answers — Chrome handed the command "
        "line to another process (SingletonLock handoff). Supervising the CDP endpoint instead "
        "of exiting; a spawned-process exit is not a browser death.",
        code,
        port,
    )
    try:
        _watch_cdp(port)
    except KeyboardInterrupt:
        # `ava stop` during the handoff watch. Chrome is no longer in this
        # process tree, so there is nothing here left to kill — just leave.
        return code
    logger.error("ava-browser: CDP :{} went unreachable — exiting so the supervisor respawns", port)
    return 1


def _launch(binary: str, args: list[str], port: int) -> None:
    """Become (POSIX) or supervise (Windows) the Chrome process. Never returns.

    POSIX — `os.execvp` replaces this process in place, so the session's
    process IS Chrome: one pid, and killing the session kills the browser. A
    handoff is invisible and harmless here, because the session's process only
    ends when the
    exec'd Chrome does.

    Windows has no exec. `os.execvp` there spawns a *new* process and exits the
    caller (measured on the `win` runner: pid 950056 -> 948684), and the pid
    `winproc.new_session` recorded belongs to the `cmd /c` that ran this
    launcher — so that cmd returns the moment this process exits. The session
    record goes dead while Chrome keeps running, `ava status` reads
    `x session / ok (http)` on a perfectly clean start, and
    `winproc.kill_session` can no longer reach Chrome because it is a descendant
    of nothing live. The orphan then holds the CDP port against every later
    launch, which the daemon's port guard turns into a permanent refusal.

    So on Windows this process stays put as Chrome's parent (`_supervise_chrome`).
    The supervised tree is `cmd -> python -> chrome.exe`: `has_session` sees the
    recorded pid alive, and `_terminate_tree` takes the whole tree down.
    """
    if not IS_WINDOWS:
        try:
            os.execvp(binary, args)  # noqa: S606 — intentional exec of the resolved Chrome binary
        except OSError as exc:
            logger.error("ava-browser exec failed: {}", exc)
            sys.exit(127)
        return  # unreachable: a successful execvp never comes back
    sys.exit(_supervise_chrome(args, port))


def main() -> None:
    # Fail loud BEFORE the log redirect so the reason is visible in the
    # session log — this also guards healthcheck-triggered respawns, not just the first
    # launch (the converge preflight only runs at `ava start`).
    assert_browser_capable()
    if _cdp_reachable(_CDP_PORT):
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
            f"ava-browser: CDP port {_CDP_PORT} already served by another Chrome; "
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
    profile = _profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    # Redirect stdout/stderr to browser.log before execvp so a Chrome crash is
    # postmortem-able (same blind-spot fix as services/milvus/daemon.py).
    log_fd = os.open(str(logs_dir() / "browser.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(log_fd)
    _launch(binary, _chrome_args(binary, _CDP_PORT, profile, _frontend_url()), _CDP_PORT)


if __name__ == "__main__":
    main()
