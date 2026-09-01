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
  healthcheck now closes this loop itself instead of naming a remedy an
  operator would have to run: it sweeps the Chrome (identity-verified ours,
  `services/browser/orphan.reap_cluster_chrome`) and rebuilds the session in
  the same round — the automated equivalent of `ava stop --stop-browser` +
  `ava start`. The profile persists, so logins survive the rebuild.
- **CDP down** (`DOWN`), session alive or not — nothing is serving. Respawn in
  the ava-browser pane via `shared.service_respawn.respawn_service` (which kills
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

from services.browser.orphan import reap_cluster_chrome
from services.browser.probe import probe_browser
from shared.cluster import session_name
from shared.config import settings
from shared.daemon_health import EXIT_PORT_TAKEN, EXIT_RESPAWN_FAILED, DaemonProbe
from shared.log import init_gateway_process
from shared.paths import run_dir
from shared.service_respawn import respawn_service
from shared.session_backend import get_backend

_log = logging.getLogger("services.healthchecks.browser")

_PORT = settings.services.browser_cdp_port  # per-cluster (cluster port block); default 9222

# How long a reported failure episode stays quiet before the next ERROR reminder.
# The condition is still logged at DEBUG every round; this only bounds how long
# the alerting surface goes without a fresh line.
_EPISODE_REMINDER_S = 6 * 3600.0


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


def main() -> None:
    init_gateway_process(name="browser-healthcheck")
    episode = _episode_reporter()
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
            _log.debug("[browser healthcheck] alive (%s), no-op", probe.detail)
            return
        # Live session, dead CDP: Chrome crashed or hung inside its own pane.
        # respawn_service kills the stale session first, so the restart applies.
    elif probe.alive:
        # Our Chrome holds the port but the supervised session is gone — the
        # shape that once waited on an operator. Close the loop here: sweep
        # the unsupervised Chrome (identity-verified ours) and rebuild the
        # session in the same round.
        _log.info(
            "[browser healthcheck] CDP port %d is served by this cluster's own Chrome (%s) "
            "but the ava-browser session is gone — sweeping the unsupervised Chrome and "
            "rebuilding the session",
            _PORT,
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
