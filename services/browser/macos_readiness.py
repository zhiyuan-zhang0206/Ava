"""Read-only macOS startup readiness for the shared headed Chrome.

Chrome obtains the profile encryption key from the logged-in user's Keychain.
The detached browser service has no usable browser until both that GUI session
and its login Keychain are ready, so starting it over SSH can otherwise create
an unusable profile process. This module only observes those prerequisites; it
never unlocks a Keychain, changes the login session, or touches Chrome data.

While readiness is absent, the daemon records a short-lived marker under
``$AVA_HOME/run``. The healthcheck uses it to distinguish a deliberate wait in
a live supervised session from a crashed browser, avoiding restart churn. The
marker also carries the one wait this module can diagnose as unfixable in
place: ``launchctl managername`` says whether the process chain runs inside the
GUI login session ("Aqua") or outside it ("Background" — an agent or SSH
chain, or a respawn from one), where no amount of waiting makes the login
Keychain readable. That case is marked ``context_missing``, and the healthcheck
reacts by relaunching the service from the GUI domain instead of retrying.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

import shared.paths
import shared.private_storage
import shared.proc
from shared.platform import IS_MACOS

try:  # `pwd` is absent on Windows, where this module remains import-safe.
    import pwd
except ImportError:  # pragma: no cover - Windows-only import shape
    pwd = None  # type: ignore[assignment]

_log = logging.getLogger("services.browser.macos_readiness")

_PROBE_TIMEOUT_S = 5.0
_READINESS_RETRY_S = 5.0
_READINESS_REPORT_INTERVAL_S = 300.0
_WAIT_MARKER_NAME = "browser-readiness-wait.json"
_WAIT_MARKER_MAX_AGE_S = 90.0


@dataclass(frozen=True)
class StartupReadiness:
    """Whether it is safe to launch headed Chrome now, plus a safe reason if not.

    ``context_missing`` marks the not-ready case that waiting cannot fix: this
    process chain runs outside the GUI login session, so the Keychain refusal
    is a property of the chain (a respawn from an agent/SSH process), not of
    the session's readiness — a relaunch through the GUI domain is required.
    """

    ready: bool
    reason: str | None = None
    context_missing: bool = False


@dataclass(frozen=True)
class BrowserWaitState:
    """A current daemon's deliberate readiness wait, read by the healthcheck.

    ``context_missing`` is the wait's unfixable-in-place flavor: the daemon
    chain is outside the GUI login session, so the healthcheck's remedy is a
    GUI-domain relaunch, never a respawn in place.
    """

    reason: str
    pid: int
    observed_at: float
    context_missing: bool = False


@dataclass(frozen=True)
class _ProbeResult:
    """Normalized bounded system-query result, including a timeout as non-zero."""

    returncode: int
    stdout: str
    stderr: str


def _run_probe(argv: list[str]) -> _ProbeResult:
    """Run a fixed read-only system query without letting a GUI prompt hang us."""
    try:
        completed = shared.proc.run_bounded(
            argv, timeout=_PROBE_TIMEOUT_S, capture_output=True, text=True
        )
    except FileNotFoundError:
        return _ProbeResult(127, "", f"{argv[0]} is unavailable")
    except subprocess.TimeoutExpired:
        return _ProbeResult(124, "", f"timed out after {_PROBE_TIMEOUT_S:.0f}s")
    return _ProbeResult(
        completed.returncode,
        str(completed.stdout or ""),
        str(completed.stderr or ""),
    )


def _current_account() -> tuple[str, Path]:
    """Current process account and its home directory on macOS.

    This is intentionally account-derived rather than inherited from shell
    environment variables, which may describe the SSH caller rather than the
    launch context of the detached service.
    """
    if pwd is None:  # Defensive only; callers gate on IS_MACOS first.
        raise RuntimeError("macOS account lookup is unavailable")
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
    if not IS_MACOS:
        return StartupReadiness(ready=True)

    account, home = _current_account()
    console = _run_probe(["/usr/bin/stat", "-f%Su", "/dev/console"])
    console_user = console.stdout.strip()
    if console.returncode != 0 or console_user != account:
        actual = console_user or "none"
        return StartupReadiness(
            ready=False,
            reason=f"no active GUI login session for {account} (console user is {actual})",
        )

    gui = _run_probe(["/bin/launchctl", "print", f"gui/{os.getuid()}"])
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
    domain = _run_probe(["/bin/launchctl", "managername"])
    managed = domain.stdout.strip()
    if domain.returncode == 0 and managed and managed != "Aqua":
        return StartupReadiness(
            ready=False,
            context_missing=True,
            reason=(
                f"this process is not in the GUI login session "
                f"(launchd managername: {managed}); the login Keychain is unreachable "
                f"from this process chain and a relaunch from the GUI domain is required"
            ),
        )

    keychain = home / "Library" / "Keychains" / "login.keychain-db"
    keychain_probe = _run_probe(["/usr/bin/security", "show-keychain-info", str(keychain)])
    if keychain_probe.returncode != 0:
        detail = keychain_probe.stderr.strip().splitlines()[-1:] or ["not available"]
        return StartupReadiness(ready=False, reason=f"login Keychain is not ready ({detail[0]})")
    return StartupReadiness(ready=True)


def _marker_path() -> Path:
    return shared.paths.run_dir() / _WAIT_MARKER_NAME


def _current_process_started_at() -> float:
    return psutil.Process(os.getpid()).create_time()


def mark_waiting(reason: str, *, context_missing: bool = False) -> None:
    """Persist the current daemon's readiness wait for its healthcheck peer.

    ``context_missing`` records whether the wait is the unfixable-in-place kind
    (this chain is outside the GUI login session), so the healthcheck can tell
    "keep waiting" apart from "relaunch through the GUI domain".

    A private, atomically replaced marker keeps a reader from observing a
    half-written JSON record. Marker failures do not change the launch gate:
    the daemon continues waiting safely, and the healthcheck falls back to its
    own bounded readiness probe when the marker is unavailable.
    """
    try:
        payload = {
            "reason": reason,
            "pid": os.getpid(),
            "started_at": _current_process_started_at(),
            "observed_at": time.time(),
            "context_missing": context_missing,
        }
        shared.private_storage.write_private_bytes(_marker_path(), json.dumps(payload).encode())
    except (OSError, psutil.Error):
        _log.warning("ava-browser: could not record macOS readiness wait state", exc_info=True)


def clear_waiting() -> None:
    """Remove this daemon's wait marker immediately before Chrome can launch."""
    try:
        _marker_path().unlink(missing_ok=True)
    except OSError:
        _log.debug("ava-browser: could not clear macOS readiness wait marker", exc_info=True)


def _owner_is_alive(pid: int, started_at: float) -> bool:
    """True only while `pid` still names the marker's original process."""
    try:
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - started_at) < 2.0
    except psutil.Error:
        return False


def waiting_state() -> BrowserWaitState | None:
    """Return a current readiness wait marker, rejecting stale or recycled owners."""
    try:
        raw = json.loads(_marker_path().read_text())
        reason = raw["reason"]
        pid = raw["pid"]
        started_at = raw["started_at"]
        observed_at = raw["observed_at"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(reason, str) or not isinstance(pid, int):
        return None
    if not isinstance(started_at, (int, float)) or not isinstance(observed_at, (int, float)):
        return None
    # Fail quiet on a malformed flag: an unreadable value must not trigger the
    # healthcheck's GUI-domain relaunch (or hide a real wait state) on its own.
    context_missing = raw.get("context_missing", False)
    if not isinstance(context_missing, bool):
        context_missing = False
    if time.time() - observed_at > _WAIT_MARKER_MAX_AGE_S:
        return None
    if not _owner_is_alive(pid, float(started_at)):
        return None
    return BrowserWaitState(reason, pid, float(observed_at), context_missing=context_missing)


def degraded_wait_state() -> StartupReadiness | None:
    """The current deliberate wait, structured; None when there is no wait.

    Marker first — it names the waiting daemon and the fact the caller acts on
    (`context_missing`). The marker is an observability optimization, not the
    launch gate itself: if its private runtime directory is unavailable, a
    healthcheck must still recognize the live daemon's deliberate wait and
    avoid killing it into a restart loop, so a direct probe answers instead.
    That fallback's `context_missing` describes the CALLER's own chain — which
    is also the chain any respawn it performs would inherit — and is
    best-effort where the marker is missing. The fallback is bounded and
    read-only; an unexpected macOS probe failure also fails safe to degraded
    rather than launching Chrome without an answer about its prerequisites.
    """
    marker = waiting_state()
    if marker is not None:
        return StartupReadiness(
            ready=False, reason=marker.reason, context_missing=marker.context_missing
        )
    try:
        readiness = probe_startup_readiness()
    except Exception:  # Healthchecks must not turn an unknown readiness state into a crash loop.
        if IS_MACOS:
            _log.exception("ava-browser: macOS readiness fallback probe failed")
            return StartupReadiness(
                ready=False, reason="macOS startup readiness could not be probed"
            )
        return None
    if readiness.ready:
        return None
    return readiness


def degraded_wait_reason() -> str | None:
    """The current wait's reason, or None — the log-line form of
    `degraded_wait_state`."""
    state = degraded_wait_state()
    if state is None:
        return None
    return state.reason or "macOS browser startup readiness is unavailable"


def wait_for_browser_startup_readiness() -> None:
    """Block the supervised daemon until macOS can safely serve Chrome's keys."""
    last_reason: str | None = None
    last_reported_at = 0.0
    while True:
        readiness = probe_startup_readiness()
        if readiness.ready:
            clear_waiting()
            return
        reason = readiness.reason or "macOS browser startup readiness is unavailable"
        mark_waiting(reason, context_missing=readiness.context_missing)
        now = time.monotonic()
        if reason != last_reason or now - last_reported_at >= _READINESS_REPORT_INTERVAL_S:
            _log.warning(
                "ava-browser DEGRADED: %s; waiting before Chrome launch and retrying every %.0fs",
                reason,
                _READINESS_RETRY_S,
            )
            last_reason = reason
            last_reported_at = now
        time.sleep(_READINESS_RETRY_S)
