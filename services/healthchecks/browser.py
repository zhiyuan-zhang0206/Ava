"""ava-browser healthcheck — called every 60s by the watchdog.

Two questions, both required, because either answer alone lies:

- **is the browser ours?** — `services/browser/probe.py` dials CDP
  (`http://127.0.0.1:<port>/json/version`) and then verifies that the process
  listening on that port is a Chrome running on THIS cluster's profile. A 200
  alone only says *a* debuggable Chrome is up, and CDP carries no field we
  control, so the identity is established from the profile + the listening
  socket instead (see that module for why CDP itself cannot answer it).
- **is it supervised?** — the `ava-browser` session process is alive, i.e. it is
  under the supervisor `ava stop` / `ava cluster update` can reach. On Windows that
  process is the launcher supervising Chrome, and it holds the session open
  across a `SingletonLock` handoff (`services/browser/daemon.py`:
  `_supervise_chrome`) — a handed-off Chrome is out of its process tree, so the
  reach claim weakens there (the known gap is recorded in
  `services/agent_runner_side/browser/browser/browser.ava.okf.md`).

A bare CDP probe was the whole check, and it cannot tell a supervised Chrome from
an orphan — nor ours from anyone's. The daemon deliberately refuses to launch
while the port is served (`services/browser/daemon.py`), so an occupant holding
the port made this healthcheck a permanent no-op: green forever, no supervised
browser, no signal.

The failure shapes get different treatment, and the split is the standard
`ProbeVerdict` one — whether a respawn can win:

- **someone else's browser holds the port** (`PORT_TAKEN`) — respawning walks
  into the daemon's refusal once every 60s, forever. Report at ERROR and exit
  with `EXIT_PORT_TAKEN`, so the watchdog's own log line carries the distinct
  code. This is the case a CDP-only probe could not even see.
- **ours, but the session is gone** — an unsupervised Chrome of our own (a
  `SingletonLock` handoff, or a Chrome started by hand on our profile). The
  healthcheck closes this loop itself instead of naming a remedy an operator
  would have to run: it sweeps the Chrome (identity-verified ours,
  `services/browser/orphan.reap_cluster_chrome`) and rebuilds the session in
  the same round — the automated equivalent of `ava stop --stop-browser` +
  `ava start`. The profile persists, so logins survive the rebuild.
- **the session is gone, whatever the probe says** — the sweep + rebuild above
  runs for EVERY session-gone shape, not only when the probe reads ALIVE. A
  probe that reads DOWN because our orphaned Chrome still holds the port with
  a wedged DevTools endpoint (the 2026-09-09 swap-pressure outage: HTTP 200,
  empty `/json/version` body, 8 minutes of CDP silence) must not fall through
  to a plain respawn — the daemon refuses the occupied port, so a respawn
  alone cannot win and only churns once per round. The reap frees the port;
  when nothing is left to reap it is a no-op and the rebuild is all that runs.
- **CDP down** (`DOWN`) with the session alive — nothing is serving (or the
  endpoint is wedged, see `services/browser/probe.py`). Respawn in the
  ava-browser pane via `shared.service_respawn.respawn_service` (which kills
  the stale session first, so a live-but-wedged pane is covered too).

## Episode-gated reporting

A persistent failure condition (a foreign occupant, a sweep that keeps failing,
a respawn that never comes up) was reported as a fresh ERROR **every round**,
which is exactly the 1.8k-errors/day storm this module caused across machine-1 and
win (2026-08-12). ERROR lines are now emitted on state CHANGE only: the first
round of an episode, each time the condition changes, and one reminder every
`_EPISODE_REMINDER_S`. Quiet rounds log the same fact at DEBUG, so the
condition stays visible in the log without re-alarming. A healthy round ends
any open episode and logs one INFO recovery line. The exit codes are unchanged
— a quiet terminal round still exits `EXIT_PORT_TAKEN` — and the watchdog
de-duplicates its own "reported failure (exit N)" line per check+code, so the
whole chain lands one ERROR per episode.

The episode record lives under `$AVA_HOME/run/healthcheck-state/browser.json`
and only ever gates REPORTING: it can never suppress a reap or a respawn, and
an unreadable record fails open toward reporting.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from services.browser import macos_readiness
from services.browser.macos_readiness import StartupReadiness
from services.browser.orphan import reap_cluster_chrome
from services.browser.probe import probe_browser
from shared.cluster import session_name
from shared.config import settings
from shared.daemon_health import EXIT_PORT_TAKEN, EXIT_RESPAWN_FAILED, DaemonProbe
from shared.log import init_gateway_process
from shared.os_autostart import relaunch_via_gui_domain
from shared.paths import run_dir
from shared.platform import IS_MACOS
from shared.service_respawn import respawn_service
from shared.session_backend import get_backend

_log = logging.getLogger("services.healthchecks.browser")

_PORT = settings.services.browser_cdp_port  # per-cluster (cluster port block); default 9222

# How long a reported failure episode stays quiet before the next ERROR reminder.
# The condition is still logged at DEBUG every round; this only bounds how long
# the alerting surface goes without a fresh line.
_EPISODE_REMINDER_S = 6 * 3600.0

# The GUI-domain context heal (macOS): a daemon chain that lost the GUI login
# session cannot read the login Keychain, and no respawn inside that chain can
# fix it — only `ava start` running in the GUI domain can. The heal stops the
# stuck session and kickstarts this cluster's autostart job; these bound it.
_CONTEXT_HEAL_MAX_ATTEMPTS = 2
# Also the "a GUI relaunch is in flight" window the session-gone branch honours.
_CONTEXT_HEAL_COOLDOWN_S = 600.0
# The stop waits no longer than this for the stuck daemon to exit gracefully
# before the backend's force fallback — a fraction of the watchdog's 90s tick.
_CONTEXT_HEAL_STOP_TIMEOUT_S = 10.0


def _probe() -> DaemonProbe:
    """Identity-verified liveness — see `services.browser.probe.probe_browser`.

    Named `_probe` like every other healthcheck's, so the probe contract's "the
    public name is the total wrapper" rule reads the same here."""
    return probe_browser(_PORT)


def _session_alive() -> bool:
    """True when the supervised ava-browser session process is still alive.

    Asked through the session backend, so it is one question on both platforms:
    the native supervisor's session record (pid + create_time) on POSIX, the
    winproc session record on Windows."""
    return get_backend().has_session(session_name("browser"))


def _restart_daemon() -> bool:
    project_root = settings.services.project_root or Path(__file__).resolve().parent.parent.parent
    return respawn_service(
        "browser",
        ".venv/bin/python -m services.browser.daemon",
        project_root,
        extra_env={"AVA_PROCESS_PROFILE": "runner"},
    )


def _sweep_and_rebuild() -> bool:
    """Sweep the unsupervised Chrome off this cluster's profile, then rebuild the
    ava-browser session — the operator's `ava stop --stop-browser` + `ava start`
    remedy, automated. The reap is identity-verified (profile + process-table
    walk; see `services/browser/orphan.py`), so it can never take down a Chrome
    that is not this cluster's.

    The reap's own exceptions are logged at DEBUG with a traceback and folded
    into a False return — the caller's episode-gated ERROR reports the failed
    heal without a new traceback per round."""
    try:
        reaped = reap_cluster_chrome()
    except Exception:
        _log.debug(
            "[browser healthcheck] sweep raised; cannot rebuild the session this round",
            exc_info=True,
        )
        return False
    if not reaped:
        # The Chrome died between the probe and the sweep — the port is free
        # already, so the rebuild is all that is left.
        _log.info(
            "[browser healthcheck] no unsupervised Chrome left to sweep; rebuilding the session"
        )
    return _restart_daemon()


@dataclass(frozen=True)
class _EpisodeRecord:
    """One failure episode's persisted bookkeeping. `condition` is a stable
    failure-class string, never "healthy" — the record only exists while an
    episode is open."""

    condition: str
    first_seen: float
    last_reported: float


class _Episode:
    """Episode-gated ERROR reporting for a healthcheck.

    `should_report(condition)` is True on the first round of a failure episode,
    when the condition changes, when the record is unreadable (fail open toward
    reporting), and once per reminder window thereafter. `mark_healthy()` clears
    an open episode and answers whether one was open — a True answer is the
    recovery event worth one INFO line.

    All bookkeeping failures are swallowed: a write that fails means the next
    round re-reports (harmless); the healthcheck must never crash on its own
    de-noising."""

    # Condition classes the healthcheck reports. Coarse on purpose — the detail
    # rides in the log message, the class only keys the episode.
    TERMINAL = "terminal"
    ORPHAN_HEAL_FAILED = "orphan-heal-failed"
    RESPAWN_FAILED = "respawn-failed"
    WAITING_FOR_MACOS_READINESS = "waiting-for-macos-readiness"
    CONTEXT_MISSING = "context-missing"

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.time,
        remind_s: float = _EPISODE_REMINDER_S,
    ) -> None:
        self._path = path
        self._now = now
        self._remind_s = remind_s

    def _read(self) -> _EpisodeRecord | None:
        try:
            data = json.loads(self._path.read_text())
            return _EpisodeRecord(
                condition=str(data["condition"]),
                first_seen=float(data["first_seen"]),
                last_reported=float(data["last_reported"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write(self, record: _EpisodeRecord) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(
                    {
                        "condition": record.condition,
                        "first_seen": record.first_seen,
                        "last_reported": record.last_reported,
                    }
                )
            )
        except OSError:
            _log.debug("[browser healthcheck] episode record write failed; re-reporting next round")

    def should_report(self, condition: str) -> bool:
        """True when this round's `condition` deserves a fresh ERROR line."""
        now = self._now()
        stored = self._read()
        if stored is None:
            self._write(_EpisodeRecord(condition=condition, first_seen=now, last_reported=now))
            return True
        if stored.condition != condition:
            self._write(_EpisodeRecord(condition=condition, first_seen=now, last_reported=now))
            return True
        if now - stored.last_reported >= self._remind_s:
            self._write(
                _EpisodeRecord(condition=condition, first_seen=stored.first_seen, last_reported=now)
            )
            return True
        return False

    def mark_healthy(self) -> bool:
        """End an open episode; True when one was open (a recovery to report)."""
        stored = self._read()
        if stored is None:
            return False
        with contextlib.suppress(OSError):
            self._path.unlink()
        return True


def _episode_reporter() -> _Episode:
    return _Episode(run_dir() / "healthcheck-state" / "browser.json")


@dataclass(frozen=True)
class _ContextHealRecord:
    """One context-heal episode's persisted bookkeeping."""

    attempts: int
    last_attempt_at: float


class _ContextHeal:
    """Bookkeeping for the GUI-domain context heal (macOS only).

    The condition it bounds: the readiness wait says the ava-browser daemon
    chain is outside the GUI login session (`context_missing`) — nothing an
    in-place respawn can fix, because the wrong launchd domain survives every
    relaunch from this chain. The heal stops the stuck session and kickstarts
    this cluster's autostart job, whose `ava start` runs in the GUI domain.

    The record keeps that bounded: at most ``max_attempts`` stop + kick rounds,
    spaced by ``cooldown_s``, so a condition a kick cannot fix (no GUI login,
    an unregistered autostart job) cannot kick-storm. A record younger than the
    cooldown also means "a GUI relaunch is in flight": a session-gone round
    inside that window must NOT rebuild the session in this process's own
    context-less chain, or `ava start` would find a live session and skip it —
    undoing the heal and leaving the browser broken while looking supervised.

    All bookkeeping failures fall back to acting (or to deferring to the
    in-flight relaunch): de-noising must never be why a stuck browser stays
    stuck, and a failed write only means the next round may attempt again.
    """

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] = time.time,
        cooldown_s: float = _CONTEXT_HEAL_COOLDOWN_S,
        max_attempts: int = _CONTEXT_HEAL_MAX_ATTEMPTS,
    ) -> None:
        self._path = path
        self._now = now
        self._cooldown_s = cooldown_s
        self._max_attempts = max_attempts

    def _read(self) -> _ContextHealRecord | None:
        try:
            data = json.loads(self._path.read_text())
            return _ContextHealRecord(int(data["attempts"]), float(data["last_attempt_at"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write(self, record: _ContextHealRecord) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"attempts": record.attempts, "last_attempt_at": record.last_attempt_at})
            )
        except OSError:
            _log.debug(
                "[browser healthcheck] context-heal record write failed; retrying next round"
            )

    def exhausted(self) -> bool:
        """True once this episode has spent its attempt budget."""
        record = self._read()
        return record is not None and record.attempts >= self._max_attempts

    def in_flight(self) -> bool:
        """True while a just-kicked GUI relaunch may still be landing."""
        record = self._read()
        return record is not None and self._now() - record.last_attempt_at < self._cooldown_s

    def due(self) -> bool:
        """True when another stop + kick may run now."""
        if self.exhausted():
            return False
        record = self._read()
        return record is None or self._now() - record.last_attempt_at >= self._cooldown_s

    def record_attempt(self) -> None:
        """Count a stop + kick round as spent, before it runs (a crash mid-round
        must not turn into a per-round retry)."""
        record = self._read()
        attempts = 1 if record is None else record.attempts + 1
        self._write(_ContextHealRecord(attempts, self._now()))

    def clear(self) -> None:
        """End the episode — the chain is in the right context again."""
        with contextlib.suppress(OSError):
            self._path.unlink(missing_ok=True)


def _context_heal_reporter() -> _ContextHeal:
    return _ContextHeal(run_dir() / "healthcheck-state" / "browser-context-heal.json")


def _heal_context_missing(episode: _Episode, heal: _ContextHeal, wait: StartupReadiness) -> None:
    """React to a daemon chain outside the GUI login session (macOS).

    Stop the stuck ava-browser session (so the GUI-domain `ava start` does not
    skip a live session and walk away), then kickstart this cluster's autostart
    job. Episodes are bounded by `_ContextHeal`: a couple of attempts, then one
    episode-gated ERROR carrying the manual recipe.
    """
    if heal.exhausted():
        if episode.should_report(_Episode.CONTEXT_MISSING):
            _log.error(
                "[browser healthcheck] browser NOT REVIVABLE by this unit: the ava-browser "
                "chain is outside the GUI login session (%s) and the GUI-domain relaunch did "
                "not fix it after %d attempts — log into the GUI session and run `ava stop` + "
                "`ava start` there (a missing autostart job re-registers on the next `ava "
                "start`; see logs/autostart.log)",
                wait.reason,
                _CONTEXT_HEAL_MAX_ATTEMPTS,
            )
        else:
            _log.debug(
                "[browser healthcheck] still outside the GUI login session; manual fix reported "
                "this episode"
            )
        return
    if not heal.due():
        _log.debug(
            "[browser healthcheck] context heal already attempted; waiting for the GUI-domain "
            "relaunch to land"
        )
        return
    heal.record_attempt()
    try:
        stopped, _mode = get_backend().kill_session(
            session_name("browser"),
            graceful=True,
            timeout=_CONTEXT_HEAL_STOP_TIMEOUT_S,
            expected=True,
        )
    except Exception as exc:  # a healthcheck must not crash on its own heal
        _log.error(
            "[browser healthcheck] context heal: stopping the context-less ava-browser session "
            "raised %s: %s",
            type(exc).__name__,
            exc,
        )
        return
    if not stopped:
        _log.error(
            "[browser healthcheck] context heal: the context-less ava-browser session survived "
            "its stop; not kicking a GUI relaunch (it would skip the live session)"
        )
        return
    try:
        ok, detail = relaunch_via_gui_domain()
    except Exception as exc:  # a healthcheck must not crash on its own heal
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    if ok:
        _log.info(
            "[browser healthcheck] context heal: stopped the context-less ava-browser session "
            "and relaunched it via the GUI domain (%s)",
            detail,
        )
    else:
        _log.error(
            "[browser healthcheck] context heal: stopped the stuck session but could not relaunch "
            "it via the GUI domain (%s)",
            detail,
        )


def main() -> None:
    init_gateway_process(name="browser-healthcheck")
    episode = _episode_reporter()
    heal = _context_heal_reporter()
    probe = _probe()
    if probe.terminal:
        # Asked before the session question on purpose: whoever holds the port,
        # our own session being alive does not make a respawn able to bind it.
        if episode.should_report(_Episode.TERMINAL):
            _log.error(
                "[browser healthcheck] browser NOT REVIVABLE by this unit (%s) — not respawning; "
                "an operator must quit that Chrome (or move this cluster's AVA_BROWSER_CDP_PORT) "
                "before the browser service can start.",
                probe.detail,
            )
        else:
            _log.debug(
                "[browser healthcheck] browser NOT REVIVABLE (%s) — reported this episode, "
                "quiet round",
                probe.detail,
            )
        sys.exit(EXIT_PORT_TAKEN)
    if _session_alive():
        if probe.alive:
            if episode.mark_healthy():
                _log.info("[browser healthcheck] browser recovered (%s)", probe.detail)
            heal.clear()
            _log.debug("[browser healthcheck] alive (%s), no-op", probe.detail)
            return
        wait = macos_readiness.degraded_wait_state()
        if wait is not None:
            if IS_MACOS and wait.context_missing:
                _heal_context_missing(episode, heal, wait)
                return
            heal.clear()  # a context-healthy wait ends any open heal episode
            wait_reason = wait.reason or "macOS startup readiness is unavailable"
            if episode.should_report(_Episode.WAITING_FOR_MACOS_READINESS):
                _log.warning(
                    "[browser healthcheck] browser DEGRADED: waiting for macOS startup "
                    "readiness (%s); keeping the live ava-browser session for retry",
                    wait_reason,
                )
            else:
                _log.debug(
                    "[browser healthcheck] browser still waiting for macOS startup readiness "
                    "(%s); reported this episode",
                    wait_reason,
                )
            return
        # Live session, dead CDP: Chrome crashed or hung inside its own pane.
        # respawn_service kills the stale session first, so the restart applies.
    else:
        # The supervised session is gone. Whether the probe reads ALIVE (our
        # unsupervised Chrome holding the port — a SingletonLock handoff, or
        # one started by hand) or DOWN (nothing serving, or the 2026-09-09
        # swap-pressure shape: our orphaned Chrome still holding the port with
        # a wedged DevTools endpoint), the remedy is the same: sweep this
        # cluster's Chrome — identity-verified ours, a no-op when none is
        # left — and rebuild the session. A plain respawn cannot win while an
        # orphan still holds the port, because the daemon refuses to launch a
        # second Chrome on it (and launching would collide on the profile
        # SingletonLock anyway); the sweep is what frees the port.
        if heal.in_flight():
            # A GUI-domain relaunch was just kicked after a context-less wait:
            # rebuilding here would put the session back in THIS chain's wrong
            # context, and the GUI `ava start` would then skip the live session.
            _log.debug(
                "[browser healthcheck] ava-browser session gone; a GUI-domain relaunch was just "
                "kicked — deferring the in-context rebuild so the relaunch is not undone"
            )
            return
        _log.info(
            "[browser healthcheck] ava-browser session gone (%s) — sweeping the "
            "unsupervised Chrome (identity-verified ours) and rebuilding the session",
            probe.detail,
        )
        if _sweep_and_rebuild():
            _log.info(
                "[browser healthcheck] unsupervised Chrome swept; ava-browser session rebuilt"
            )
            return
        if episode.should_report(_Episode.ORPHAN_HEAL_FAILED):
            _log.error(
                "[browser healthcheck] sweep + session rebuild FAILED — manual intervention "
                "needed; retrying each round"
            )
        else:
            _log.debug("[browser healthcheck] sweep + rebuild still failing; reported this episode")
        sys.exit(EXIT_RESPAWN_FAILED)
    _log.info("[browser healthcheck] dead (%s), restarting...", probe.detail)
    if _restart_daemon():
        _log.info("[browser healthcheck] daemon restarted")
        return
    if episode.should_report(_Episode.RESPAWN_FAILED):
        _log.error("[browser healthcheck] restart FAILED — manual intervention needed")
    else:
        _log.debug("[browser healthcheck] restart still failing; reported this episode")
    sys.exit(EXIT_RESPAWN_FAILED)


if __name__ == "__main__":
    main()
