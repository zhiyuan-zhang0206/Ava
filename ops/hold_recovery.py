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


def _new_recovery_log() -> Path:
    """A fresh `$AVA_HOME/logs/hold-recover-<epoch>.log`, old siblings trimmed.

    Shares the updater family's log directory and retention
    (`ops.cluster_deploy._new_update_log`); imported lazily because this module
    is reachable from the watchdog tick, which must not drag the whole deploy
    control flow into its import graph.
    """
    from ops.cluster_deploy import _new_update_log

    return _new_update_log(SERVICE)
