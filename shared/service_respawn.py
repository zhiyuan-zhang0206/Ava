"""Shared `_restart` helper for healthchecks — start a service in a named background session.

Previously the healthchecks (gateway / scheduler / labeler / restarter)
each used ``subprocess.Popen(start_new_session=True)`` to
spawn a daemon. Problem: the detached child gets PPID=1 and is fully
detached from the original session. ``ava cluster update``
graceful kill cannot reach it; zombies remain holding the port and block
the next start (we observed :8000 held by an old uvicorn worker; the
gateway then booted with ``[Errno 48] Address already in use``).
``ava status`` showed a live session but http down, also inconsistent.

Fix: the healthchecks share this helper; the daemon enters the same
``ava-<svc>`` session as ``ava start`` — the service session backend's
record namespace (native supervisor on POSIX, winproc on Windows) —
blending into the ops contract.

``respawn_and_verify`` wraps it for daemons that expose ``/healthz``: a
successful spawn is not evidence the daemon came up, so the
respawn is only reported as a success once a probe confirms it.

``run_keepalive`` is the tier above — the shared body of every daemon
healthcheck's ``main()``, holding the one policy that decides between "no-op",
"respawn", "back off and retry later", and "report and stop because no respawn
can win". A condition a respawn cannot cure (GCS unreachable for hours, ENOSPC
crash-loop) used to earn one doomed kill+restart per watchdog round forever;
the policy now backs off exponentially and, past ``breaker_rounds`` consecutive
non-alive rounds, holds with a single alert until a round probes alive.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from shared.cluster import session_name
from shared.config import settings
from shared.daemon_health import EXIT_PORT_TAKEN, DaemonProbe
from shared.paths import prod_service_checkout_error
from shared.platform import raise_fd_limit
from shared.source_switch import is_switching

_log = logging.getLogger("shared.service_respawn")

# How long to wait for a respawned daemon to answer its own /healthz. Covers
# interpreter cold start + the daemon's pre-bind schema check (a DB round trip);
# a healthy respawn typically confirms in 2-4s, so this ceiling only bites when
# the daemon is genuinely failing to come up. The watchdog runs healthchecks
# sequentially in a worker thread, so a failing daemon delays that capability's
# remaining checks by at most this much, once per 60s round.
_VERIFY_DEADLINE_S = 20.0
_VERIFY_INTERVAL_S = 0.5

_consecutive_probe_failures: dict[str, int] = {}
"""Per-watchdog-process consecutive rounds WITHOUT a probe-alive verdict, keyed by
service label. One counter drives both the respawn threshold and the circuit
breaker (task #1941's "N rounds without probe-alive"): it resets only on an
alive verdict (or a terminal one), never on a respawn attempt, so a daemon that
comes back dead every round still accumulates toward the breaker."""
_respawn_attempts: dict[str, int] = {}
"""Respawn attempts since the last alive verdict — the exponent of the 2^n backoff."""
_next_respawn_at: dict[str, float] = {}
"""Monotonic deadline of the next allowed respawn, keyed by service label. A round
before it still probes, but skips the respawn."""
_breaker_hold_since: dict[str, float] = {}
"""Presence = the respawn breaker is open for that label; value = monotonic time it
opened, so the per-round hold WARNING can report its age."""
_keepalive_state_lock = threading.Lock()


def _monotonic() -> float:
    """Wall-independent clock for backoff deadlines — a module seam so tests can
    advance time without sleeping."""
    return time.monotonic()


def _reset_keepalive_state(label: str) -> None:
    """Full per-label reset: failure count, backoff exponent + deadline, breaker."""
    with _keepalive_state_lock:
        _consecutive_probe_failures.pop(label, None)
        _respawn_attempts.pop(label, None)
        _next_respawn_at.pop(label, None)
        _breaker_hold_since.pop(label, None)


def _reset_consecutive_probe_failures(label: str) -> None:
    """Legacy reset entry point (the tests/services/test_healthcheck_gateway.py
    fixture); resets the whole per-label keepalive state."""
    _reset_keepalive_state(label)


def _record_consecutive_probe_failure(label: str) -> int:
    with _keepalive_state_lock:
        failures = _consecutive_probe_failures.get(label, 0) + 1
        _consecutive_probe_failures[label] = failures
        return failures


def _record_respawn_attempt(label: str) -> int:
    with _keepalive_state_lock:
        attempts = _respawn_attempts.get(label, 0) + 1
        _respawn_attempts[label] = attempts
        return attempts


def _respawn_attempt_count(label: str) -> int:
    with _keepalive_state_lock:
        return _respawn_attempts.get(label, 0)


def _backoff_remaining(label: str) -> float:
    """Seconds until the next respawn is due (0 when none is scheduled or it is due)."""
    with _keepalive_state_lock:
        deadline = _next_respawn_at.get(label)
    if deadline is None:
        return 0.0
    return max(0.0, deadline - _monotonic())


def _open_breaker(label: str) -> bool:
    """Open the respawn breaker for `label`. Returns True only when THIS call opened
    it (it was closed), so the caller alerts exactly once per hold episode."""
    with _keepalive_state_lock:
        if label in _breaker_hold_since:
            return False
        _breaker_hold_since[label] = _monotonic()
        return True


def _hold_age(label: str) -> float | None:
    """Seconds since the breaker opened; None while the breaker is closed."""
    with _keepalive_state_lock:
        since = _breaker_hold_since.get(label)
    return None if since is None else _monotonic() - since


def respawn_service(
    service: str,
    cmd: str,
    repo: Path,
    *,
    checkout: Path | None = None,
    extra_env: dict[str, str] | None = None,
    force: bool = False,
    graceful_timeout_s: float | None = None,
) -> bool:
    """Idempotent restart: kill any stale session + launch through the service backend.

    Args:
        service: bare service kebab (e.g. ``gateway``, ``labeler``); the
            real session name is composed as ``ava-<service>``.
        cmd: shell command run in the session (the backend wraps it in
            ``cd <repo> && <venv activation> && exec <cmd>`` under ``bash -lc``,
            so the respawned daemon — not a wrapper shell — is the supervised pid)
        repo: directory the session starts in
        checkout: the checkout whose code this service runs, when that is not
            ``repo`` itself. The launch-site guard asks which checkout may launch
            this unit's services (Task #966) — a different question from where the
            process starts, and they part ways for a service whose working
            directory is a subdirectory of its checkout. Defaults to ``repo``.
        extra_env: layered onto the forwarded env (wins over any existing key).
            Used to inject per-process markers like AVA_PROCESS_PROFILE.
        force: launch even while the source tree is mid-switch (the
            watchdog-probe's contract — dumb revival that ignores every gate,
            see `cli/commands/_cluster_watchdog_probe.py`). Default False.
        graceful_timeout_s: when set, ask the existing session to stop and
            wait no longer than this many seconds before the backend verifies
            its SIGKILL fallback. Limited to ten seconds so a watchdog round
            never spends its whole budget on one stuck daemon.

    Returns:
        ``True`` = the backend accepted the launch; ``False`` = the launch
        failed, was held back by the source-switch window, or the checkout is
        not allowed to launch this unit's services (Task #966).
    """
    err = prod_service_checkout_error(checkout if checkout is not None else repo)
    if err:
        _log.error("[service_respawn] %s", err)
        return False
    if not force and is_switching():
        # An update is mid-checkout: the tree is being replaced file by file, so
        # a launch now could import a half-written module (or the daemon could
        # read a command shape from a torn source). Hold back — the update's own
        # `ava start` runs on the verified tree and relaunches every service,
        # and the caller's probe retries next round if the start did not.
        _log.info(
            "[service_respawn] source tree is mid-switch (an update is in flight); "
            "holding back the respawn of %s — the update's own `ava start` relaunches it",
            service,
        )
        return False
    if graceful_timeout_s is not None and not 0 < graceful_timeout_s <= 10.0:
        raise ValueError("graceful_timeout_s must be greater than 0 and at most 10 seconds")
    # Raise the fd ceiling before the child is spawned: a respawn can be driven
    # from the launchd watchdog-probe (256-fd ceiling), and the child inherits
    # this process's limit. The old runtime-setup did this alongside pointing
    # the session at the cluster socket; the socket half is dead since the
    # service backend needs no socket, so only the raise survives.
    raise_fd_limit(65536)
    session = session_name(service)
    from shared.session_backend import get_backend
    from shared.session_env import forward_env_dict

    backend = get_backend()
    # Kill any stale session first (the daemon process is dead but the session may
    # still be there); idempotent. The collector opts into the backend's bounded
    # graceful ladder, while every other restart keeps its existing immediate
    # force-kill behavior. A survivor cannot be relaunched over: a second session
    # would only disguise the original port holder as a launch failure.
    if graceful_timeout_s is None:
        stopped, _mode = backend.kill_session(session, graceful=False)
    else:
        stopped, _mode = backend.kill_session(session, graceful=True, timeout=graceful_timeout_s)
    if not stopped:
        _log.error(
            "[service_respawn] %s survived its session stop; refusing a replacement launch", session
        )
        return False
    # Forward AVA_* / bootstrap env so the respawned daemon gets this unit's
    # config, not a frozen (possibly other-cluster) server env; merge extra_env
    # (per-process markers like AVA_PROCESS_PROFILE) on top. The backend wraps
    # the command in `bash -lc` and re-activates the venv inside it, so PATH /
    # venv semantics match the old session path (the login profile rebuilds PATH
    # and drops a forwarded venv prefix — see venv_activation_prefix), then
    # `exec`s into it so a respawned daemon is signalable (see exec_into).
    env = forward_env_dict()
    if extra_env:
        env.update(extra_env)
    return backend.new_session(session, cmd, repo, env=env)


def respawn_and_verify(
    service: str,
    cmd: str,
    repo: Path,
    *,
    verify: Callable[[], DaemonProbe],
    deadline_s: float = _VERIFY_DEADLINE_S,
    interval_s: float = _VERIFY_INTERVAL_S,
    extra_env: dict[str, str] | None = None,
    graceful_timeout_s: float | None = None,
) -> DaemonProbe:
    """Respawn `service`, then poll `verify` until the daemon proves it is up.

    ``respawn_service`` returning True means only that the session backend
    accepted the command — the spawned process may die milliseconds later on
    ``[Errno 48] Address already in use``, an import error, or a schema-drift
    exit. A healthcheck that reports success on that return value tells the
    operator the opposite of what happened: during the 2026-07-24 outage the
    restarter healthcheck logged "daemon restarted successfully" nine times in a
    row while every one of those daemons crashed on a port an impostor held.

    So the respawn is reported by the probe, not by the spawn. Returns the last
    ``DaemonProbe``; the caller logs its ``detail`` either way and decides
    whether a failure is fatal. Fail-fast, not fail-quiet: the caller must not
    swallow a not-alive verdict.

    **A terminal verdict ends the poll immediately.** Once the probe reports
    ``PORT_TAKEN`` the port is held by a daemon outside this cluster's reach, so
    the remaining seconds of polling are spent waiting for a process that will
    not yield — the respawned daemon has already died on the bound port. Callers
    should not reach here with a terminal verdict at all (they check before
    respawning), but an occupant can also appear *during* the respawn, and
    burning the full deadline on it is what turned a 60s watchdog round into ~45s
    of doomed work on the 2026-07-29 `win`/WSL box.
    """
    if not respawn_service(
        service,
        cmd,
        repo,
        extra_env=extra_env,
        graceful_timeout_s=graceful_timeout_s,
    ):
        return DaemonProbe.down(f"session launch for {session_name(service)} failed")
    deadline = time.monotonic() + deadline_s
    probe = verify()
    while not probe.alive and not probe.terminal and time.monotonic() < deadline:
        time.sleep(interval_s)
        probe = verify()
    return probe


def run_keepalive(
    label: str,
    log: logging.Logger,
    *,
    probe: Callable[[], DaemonProbe],
    respawn: Callable[[], DaemonProbe],
    consecutive_failures_before_respawn: int = 1,
) -> None:
    """The whole body of a daemon healthcheck's ``main()`` — probe, then act once.

    Every ``/healthz``-probed daemon healthcheck (gateway, ops, restarter,
    labeler, heartbeat, memory-indexer, events-maintenance, task-maintenance)
    ran a hand-copied version of this. The copies are why the doomed-respawn loop
    had to be fixed in eight places, or in seven and forgotten in one; the policy
    below is now stated once.

    Three outcomes, from the probe's verdict — never from reading its ``detail``:

    - **alive** — debug line, no-op. The common case, once every 60s per service.
    - **terminal** (``PORT_TAKEN``) — ERROR line naming the occupant, then
      ``SystemExit(EXIT_PORT_TAKEN)``. **No respawn.** A respawn cannot free a
      port another unit's daemon holds, so attempting one every round is not
      remediation: on the `win` box it respawned the restarter (~22s) and ops
      (~23s) forever, which is what set that host's ~2-minute watchdog cadence
      for hours. This is loud-and-stop, not silent-give-up — the ERROR repeats
      every round and the distinct exit code reaches the watchdog's own log line
      ("healthcheck <x> reported failure (exit 3)"), because a healthcheck that
      quietly declines to heal is the shape of the 98-minute outage.
    - **down** — info line, respawn after the configured consecutive-failure
      threshold, then report what the *probe* says of the respawn. A respawn that
      never verifies alive schedules the exponential backoff and returns (no
      ``SystemExit`` — the round's failure signal is the WARNING naming the next
      attempt, and later the breaker alert); once ``breaker_rounds`` consecutive
      non-alive rounds pass, the breaker holds — see the backoff/breaker
      paragraph below.

    **Backoff and circuit breaker** (task #1941 — the third incident of the same
    shape: #920 ENOSPC crash-loop, #903/3962 heartbeat, #927 GCS-unreachable 2h+
    all "restart cannot cure it" conditions, each respawned every 60s forever).
    After a respawn that failed to verify, the next attempt is delayed by
    ``base * 2^n`` (base = the watchdog round interval, `watchdog_interval_seconds`;
    cap = `watchdog_respawn_backoff_cap_seconds`, default 30min) — the window still
    probes every round, but skips the respawn. Once ``breaker_rounds``
    (`watchdog_respawn_breaker_rounds`, default 5) consecutive rounds have gone by
    without a probe-alive verdict, the breaker opens: respawns stop and hold, the
    round logs a WARNING carrying the hold age, and ONE `respawn_breaker_open`
    event fires through the unified events pipeline (never one per round). Any
    probe-alive round — or a verified-alive respawn — resets everything: failure
    count, backoff exponent and deadline, and the breaker.

    ``breaker_rounds`` must be configured strictly greater than
    ``consecutive_failures_before_respawn``: the breaker check runs before the
    threshold branch, so an equal value would open the breaker on the very round
    the threshold is met and hold without a single respawn attempt (rejected at
    validation).

    One consequence: a failed respawn no longer raises ``SystemExit``
    (``EXIT_RESPAWN_FAILED`` remains the browser healthcheck's own exit code —
    it does not use `run_keepalive`). It schedules the backoff and returns —
    the round's state lives in the WARNING
    lines and the breaker alert, and the next round probes again instead of the
    watchdog re-driving a doomed respawn.

    Raises ``SystemExit`` rather than returning a code: these mains are entry
    points, run standalone by an OS scheduler and in-thread by the watchdog
    (which catches ``SystemExit`` explicitly for exactly this reason).

    Consecutive-failure state lives in this watchdog process, which is correct
    for its long-lived daemon loop. A standalone one-shot invocation starts
    without history and therefore cannot apply a threshold across rounds.
    """
    if consecutive_failures_before_respawn < 1:
        raise ValueError("consecutive_failures_before_respawn must be at least 1")
    breaker_rounds = settings.services.watchdog_respawn_breaker_rounds
    if breaker_rounds < 1:
        raise ValueError("watchdog_respawn_breaker_rounds must be at least 1")
    if breaker_rounds <= consecutive_failures_before_respawn:
        # A breaker that trips the round the threshold is met — or before — would
        # hold without a single respawn attempt: the breaker check runs before the
        # threshold branch, so with breaker_rounds == threshold the round that
        # would respawn instead opens the breaker. A dead configuration, not a
        # policy.
        raise ValueError(
            "watchdog_respawn_breaker_rounds must be greater than "
            "consecutive_failures_before_respawn"
        )
    backoff_base_s = settings.services.watchdog_interval_seconds
    backoff_cap_s = settings.services.watchdog_respawn_backoff_cap_seconds

    result = probe()
    if result.alive:
        # The one event that heals everything: any probe-alive round resets the
        # failure count, the backoff exponent + deadline, and the breaker.
        _reset_keepalive_state(label)
        log.debug("[%s healthcheck] daemon alive (%s), no-op", label, result.detail)
        return

    def _unrevivable(code: int, message: str, detail: str) -> NoReturn:
        """Report and exit. `NoReturn` so the two call
        sites below read as the terminating branches they are."""
        log.error(message, label, detail)
        raise SystemExit(code)

    if result.terminal:
        _reset_keepalive_state(label)
        # A foreign port owner cannot be fixed by restarting this daemon.
        _unrevivable(
            EXIT_PORT_TAKEN,
            "[%s healthcheck] daemon NOT REVIVABLE by this unit (%s) — not respawning; "
            "an operator must free the port or move this cluster's port block",
            result.detail,
        )

    failures = _record_consecutive_probe_failure(label)
    if failures >= breaker_rounds and _open_breaker(label):
        # The breaker just tripped: breaker_rounds consecutive rounds without a
        # probe-alive verdict, every respawn the backoff allowed has failed to come
        # up — a condition a respawn cannot cure (the #920/#903/#927 shape). Stop
        # respawning and hold. Alert exactly ONCE per episode through the unified
        # events pipeline (loguru event= -> events stream), not once per round; the
        # per-round WARNING below carries the continuing state.
        from shared.log import logger

        logger.warning(
            "[{label} healthcheck] respawn breaker OPEN after {rounds} rounds without "
            "a live daemon ({detail}) — holding respawns; manual intervention needed",
            event="respawn_breaker_open",
            label=label,
            rounds=failures,
            respawn_attempts=_respawn_attempt_count(label),
            detail=result.detail,
        )
    hold_age = _hold_age(label)
    if hold_age is not None:
        log.warning(
            "[%s healthcheck] daemon down, respawn held for %.0fs (%s) — not respawning",
            label,
            hold_age,
            result.detail,
        )
        return
    if failures < consecutive_failures_before_respawn:
        log.warning(
            "[%s healthcheck] probe failed (%s/%s) — not respawning yet",
            label,
            failures,
            consecutive_failures_before_respawn,
        )
        return
    backoff = _backoff_remaining(label)
    if backoff > 0:
        log.warning(
            "[%s healthcheck] daemon dead (%s) — backing off after a failed respawn; "
            "next attempt in %ds",
            label,
            result.detail,
            int(backoff),
        )
        return
    log.info("[%s healthcheck] daemon dead (%s), restarting...", label, result.detail)
    attempts = _record_respawn_attempt(label)
    delay_s = min(backoff_base_s * (2 ** (attempts - 1)), backoff_cap_s)
    with _keepalive_state_lock:
        _next_respawn_at[label] = _monotonic() + delay_s
    after = respawn()
    if after.alive:
        log.info("[%s healthcheck] daemon restarted, verified alive (%s)", label, after.detail)
        _reset_keepalive_state(label)
        return
    if after.terminal:
        # The occupant appeared between the probe and the respawn — same terminal
        # condition, reported the same way rather than as a generic failed restart.
        _reset_keepalive_state(label)
        _unrevivable(
            EXIT_PORT_TAKEN,
            "[%s healthcheck] respawn cannot bind (%s) — not retrying; "
            "an operator must free the port or move this cluster's port block",
            after.detail,
        )
    # The respawn did not come up: the next attempt is already scheduled above
    # (backoff), so this round reports and RETURNS instead of
    # exiting — the round's failure signal is the WARNING naming the next attempt
    # (and, later, the breaker alert), not an exit code the watchdog would have
    # to re-drive next round. The breaker opens once
    # breaker_rounds non-alive rounds pass.
    log.warning(
        "[%s healthcheck] daemon restart FAILED (%s) — next respawn attempt in %ds",
        label,
        after.detail,
        int(delay_s),
    )
