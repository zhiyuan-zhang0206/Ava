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
"respawn", and "report and stop because no respawn can win".
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from shared.cluster import session_name
from shared.daemon_health import EXIT_PORT_TAKEN, EXIT_RESPAWN_FAILED, DaemonProbe
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
"""Per-watchdog-process failed probe counts, keyed by service label."""
_consecutive_probe_failures_lock = threading.Lock()


def _reset_consecutive_probe_failures(label: str) -> None:
    with _consecutive_probe_failures_lock:
        _consecutive_probe_failures.pop(label, None)


def _record_consecutive_probe_failure(label: str) -> int:
    with _consecutive_probe_failures_lock:
        failures = _consecutive_probe_failures.get(label, 0) + 1
        _consecutive_probe_failures[label] = failures
        return failures


def respawn_service(
    service: str,
    cmd: str,
    repo: Path,
    *,
    checkout: Path | None = None,
    extra_env: dict[str, str] | None = None,
    force: bool = False,
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
    # still be there); idempotent. The kill's own confirmation (issue #1015) is
    # what `ok` means, so a session that outlives it is reported by the probe the
    # caller runs, not by this launch.
    backend.kill_session(session, graceful=False)
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
    if not respawn_service(service, cmd, repo, extra_env=extra_env):
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
    on_unrevivable: Callable[[], None] | None = None,
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
      threshold, then report what the *probe* says of the respawn
      (``respawn_and_verify``), exiting non-zero if it never came up.

    ``on_unrevivable`` is the caller's fallback for "this round will have no live
    daemon", run when the respawn failed to verify AND on the terminal path (where
    no respawn is attempted at all). The restarter passes its stand-in dispatch
    there: while the daemon stays down nothing else moves this host's `restarting`
    rows, and a terminal verdict is the case where that is indefinite.

    It is deliberately never called BEFORE the respawn. The restarter's stand-in
    reads the DB, and a DB outage must not be able to stand between a dead verdict
    and the respawn — that ordering is the invariant `services/healthchecks/
    restarter.py` documents, and putting the hook after every respawn attempt is
    what makes it unbreakable from here.

    Raises ``SystemExit`` rather than returning a code: these mains are entry
    points, run standalone by an OS scheduler and in-thread by the watchdog
    (which catches ``SystemExit`` explicitly for exactly this reason).

    Consecutive-failure state lives in this watchdog process, which is correct
    for its long-lived daemon loop. A standalone one-shot invocation starts
    without history and therefore cannot apply a threshold across rounds.
    """
    if consecutive_failures_before_respawn < 1:
        raise ValueError("consecutive_failures_before_respawn must be at least 1")

    result = probe()
    if result.alive:
        _reset_consecutive_probe_failures(label)
        log.debug("[%s healthcheck] daemon alive (%s), no-op", label, result.detail)
        return

    def _unrevivable(code: int, message: str, detail: str) -> NoReturn:
        """Report, run the caller's fallback, exit. `NoReturn` so the three call
        sites below read as the terminating branches they are."""
        log.error(message, label, detail)
        if on_unrevivable is not None:
            on_unrevivable()
        raise SystemExit(code)

    if result.terminal:
        _reset_consecutive_probe_failures(label)
        # No respawn at all — see ProbeVerdict. The stand-in fallback still runs:
        # this is the case where "no live daemon" lasts until a human intervenes.
        _unrevivable(
            EXIT_PORT_TAKEN,
            "[%s healthcheck] daemon NOT REVIVABLE by this unit (%s) — not respawning; "
            "an operator must free the port or move this cluster's port block",
            result.detail,
        )

    failures = _record_consecutive_probe_failure(label)
    if failures < consecutive_failures_before_respawn:
        log.warning(
            "[%s healthcheck] probe failed (%s/%s) — not respawning yet",
            label,
            failures,
            consecutive_failures_before_respawn,
        )
        return
    _reset_consecutive_probe_failures(label)
    log.info("[%s healthcheck] daemon dead (%s), restarting...", label, result.detail)
    after = respawn()
    if after.alive:
        log.info("[%s healthcheck] daemon restarted, verified alive (%s)", label, after.detail)
        return
    if after.terminal:
        # The occupant appeared between the probe and the respawn — same terminal
        # condition, reported the same way rather than as a generic failed restart.
        _unrevivable(
            EXIT_PORT_TAKEN,
            "[%s healthcheck] respawn cannot bind (%s) — not retrying; "
            "an operator must free the port or move this cluster's port block",
            after.detail,
        )
    _unrevivable(
        EXIT_RESPAWN_FAILED,
        "[%s healthcheck] daemon restart FAILED (%s) — manual intervention needed",
        after.detail,
    )
