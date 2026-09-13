"""Bounded automatic completion of a stranded update hold (task #3142).

A stranded hold (task #3132) is a maintenance hold an update leg left behind
with nothing executing under it: the host sits paused with its services down
and no owner coming back — the shape a failed `ava start` leaves inside an
updater run. `ops.controllers.stranded_pause` declares it and calls in here;
this module spends the episode's single bounded attempt by spawning the
detached session (`cli/commands/_hold_recover.py`) that re-runs the same
stop / start / resume sequence an operator would run by hand.

The mechanism is the narrow exception approved as decision D1 (2026-09-12):
only an update-armed hold, only a post-stop phase, one attempt per episode
with a cooldown, and a kill-switch. Rationale, bounds and the manual fallback
live in `conventions/graceful-maintenance.md` ("Recovering a stuck maintenance
operation"), which the drill for this feature exercises against these same
steps.
"""

from __future__ import annotations

import logging
import shlex
from datetime import datetime
from pathlib import Path

from ops import cluster_session
from ops.cluster_session import _HOLD_RECOVER_SERVICE, _REPO_ROOT, _native_arg
from shared.cluster import session_name
from shared.machine import MachineRole
from shared.session_env import venv_activation_prefix

_log = logging.getLogger(__name__)

#: The episode's budget (decision D2, conservative start): one attempt, plus a
#: cooldown so a re-declared record — or a second episode on a state that is
#: still settling — cannot spend a fresh attempt immediately.
MAX_ATTEMPTS = 1
COOLDOWN_S = 900.0

#: The post-stop phases a completion may act on. Pre-stop phases (preparing /
#: draining / drained) are deliberately absent: an incomplete drain is what
#: `maintenance resume --cancel` is for, never an automatic stop.
RECOVERABLE_PHASES = frozenset({"stopping", "stopped", "starting"})

#: Session slug (`ops.cluster_session` owns it, as it owns the updater's);
#: the session is `ava-hold-recover`. `shared.proc` sanctions it as a host of
#: an in-process host transition, exactly like the updater's detached pane —
#: the stop leg does not target it, and running stop/start inside it is the
#: sanctioned shape.
SERVICE = _HOLD_RECOVER_SERVICE


def recovery_session() -> str:
    """The detached session name a completion attempt runs in."""
    return session_name(SERVICE)


def spawn_hold_recovery(*, holder: str, acquired_at: datetime) -> Path:
    """Spawn the detached completion session; return the log path it writes.

    The caller has already reserved the attempt
    (`host_deploy_state.reserve_stranded_recovery`), so an attempt is spent
    even if the spawn itself fails: the budget belongs to the episode, and a
    session backend that cannot start a session is a condition the next
    episode inherits rather than one to retry unboundedly here.

    The spawned command is the process entry `python -m
    cli.commands._hold_recover` with the hold's exact `(holder, acquired_at)`
    capability — the same argv shape `spawn_update` / `spawn_restart` use, so
    the POSIX shell spelling and the Windows `cmd /c` spelling carry the same
    work and `forward_env_dict` hands over the venv-activated environment.

    Raises:
        OrchestrationSpawnFailed: the session backend declined to start it.
    """
    session = recovery_session()
    log_path = _new_recovery_log()
    stamp = acquired_at.isoformat()
    inner_cmd = (
        f"{{ echo {shlex.quote(f'[hold-recover] holder={holder} acquired_at={stamp}')}; "
        f"cd {shlex.quote(str(_REPO_ROOT))} && {venv_activation_prefix()}"
        f"python -m cli.commands._hold_recover --operation {shlex.quote(holder)} "
        f"--acquired-at {shlex.quote(stamp)}; "
        f'echo "[session-exit] rc=$?"; }} '
        f"2>&1 | tee -a {shlex.quote(str(log_path))}"
    )
    native_cmd = (
        f"echo [hold-recover] starting"
        f" & python -m cli.commands._hold_recover --operation {_native_arg(holder)}"
        f" --acquired-at {_native_arg(stamp)}"
    )
    cluster_session._spawn_detached_session(session, shell_cmd=inner_cmd, native_cmd=native_cmd)
    _log.warning(
        "[hold-recovery] spawned stranded-hold completion session %s holder=%s log=%s",
        session,
        holder,
        log_path,
    )
    return log_path


def maybe_complete_stranded_hold(*, role: MachineRole) -> None:
    """Spend this episode's one bounded attempt at completing the hold (task #3142).

    Moved here from `ops.controllers.stranded_pause` (task #3270) so the policy
    and the mechanism live together; the caller owns the verdict gate -- only a
    `stranded` verdict carrying a *failed updater leg* reaches this function.

    - **Narrow** — only a post-stop phase, and never in the gateway capability's
      watchdog round: the gateway watchdog does not initiate a completion, while
      a unit that also serves `agent-runner` completes the same hold through
      that capability's round. `role` is the capability of the ROUND being run
      (`services/watchdog/daemon.py` runs one per capability), not a statement
      about the machine.
    - **Bounded** — one attempt per episode: the budget is a compare-and-set in
      the host's durable record (`reserve_stranded_recovery`), so two racing
      deciders cannot both spawn a leg and a spent budget is never refunded.
    - **Observable, and switchable** — the spawn is logged, the outcome lands in
      the record, and `settings.gateway.stranded_hold_recovery` turns it off.

    A spawn that fails still spends the attempt: the episode's budget is what
    bounds the mechanism's exposure, not the backend's success.
    """
    from shared.config import settings

    if not settings.gateway.stranded_hold_recovery:
        return
    if role == "gateway":
        return
    held = _held_generation()
    if held is None:
        return
    holder, acquired_at, phase = held
    if phase not in RECOVERABLE_PHASES:
        return
    from shared.host_deploy_state import finish_stranded_recovery, reserve_stranded_recovery

    attempt = reserve_stranded_recovery(
        max_attempts=MAX_ATTEMPTS,
        cooldown_s=COOLDOWN_S,
        note=f"attempt reserved (phase={phase})",
    )
    if attempt is None:
        return  # budget spent, inside the cooldown, or the record just cleared
    try:
        log_path = spawn_hold_recovery(holder=holder, acquired_at=acquired_at)
    except Exception as exc:
        _log.error(
            "[hold-recovery] stranded-hold completion session could not start "
            "(the attempt is spent): %r",
            exc,
        )
        try:
            finish_stranded_recovery(f"spawn failed: {exc!r}"[:500])
        except Exception:
            _log.warning("[hold-recovery] could not record the failed spawn", exc_info=True)
        return
    _log.warning(
        "[hold-recovery] stranded hold: bounded completion attempt #%d started "
        "(phase=%s holder=%s) — log %s",
        attempt,
        phase,
        holder,
        log_path,
    )


def _held_generation() -> tuple[str, datetime, str] | None:
    """This host's `(holder, acquired_at, phase)`, or None when no hold stands."""
    from shared import pause_owner

    current = pause_owner.read()
    if current.status != "paused" or current.maintenance is None:
        return None
    if current.holder is None or current.acquired_at is None:
        return None
    return current.holder, current.acquired_at, current.maintenance.phase


def _new_recovery_log() -> Path:
    """A fresh `$AVA_HOME/logs/hold-recover-<epoch>.log`, old siblings trimmed.

    Shares the updater family's log directory and retention
    (`ops.cluster_deploy._new_update_log`); imported lazily because this module
    is reachable from the watchdog tick, which must not drag the whole deploy
    control flow into its import graph.
    """
    from ops.cluster_deploy import _new_update_log

    return _new_update_log(SERVICE)
