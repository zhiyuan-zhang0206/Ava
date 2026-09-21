"""One bounded caller-side recovery for a half-done update stop (task #3942).

The agent-runner self-update stops this host's services (step 4) and starts them
again on the fresh tree (step 5). When the stop exited non-zero the leg returned
that rc unchanged: the host sat half-stopped with its maintenance hold still
held, and the only completions were out-of-band (the pause controller's
stranded-hold controller and its one bounded attempt, task #3142; the
OS-scheduled hold watchdog, task #3887) or an operator's `ava start`. This
module adds the caller's own bounded arm — the POSIX mirror of the cmd.exe
ladder's recovery `ava start` on Windows (`ops/_update_shell.py`):

- **Narrow.** It attempts the leg's own step-5 start exactly once, and only when
  this leg can prove the host is genuinely mid-transition and nothing else is
  executing it: the maintenance hold is readable and is the exact generation
  this leg stopped under, its phase is a post-stop one (`stopping`/`stopped` —
  `starting` means a start is already in flight and defers), this updater's
  handoff marker is running with a live owner, and the host is not already a
  declared stranded hold.
- **Bounded.** One fresh `ava start --persist-services --updater-telemetry`
  child (the same internal-start shape as step 5), killed at
  `settings.gateway.stop_incomplete_recovery_timeout_seconds`.
- **Observable and switchable.** `settings.gateway.stop_incomplete_recovery`
  turns it off; every attempt prints one `[updater] stop-recovery:` verdict line
  — `ops.updater_outcome` carries it as the run's detail, the durable record on
  this path. Each attempt also calls `record_detail`, but that lands only when
  an ambient rollout-telemetry collector is active in the process; the updater
  leg runs without one today, so the printed line is what persists.
- **Read-only on the shared state.** The caller arm never writes
  `host_deploy_state`: each actor gets its own single attempt (design v0.2 Q1'),
  so a failed caller attempt must not swallow the OS arm's one bounded
  completion.

Every refusal and every failure is non-fatal: the verdict says so, and the leg
returns its stop rc unchanged while the delegated paths (the stranded-hold
completion, the watchdog respawn, a manual `ava start`) take over.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from subprocess import TimeoutExpired

from shared.rollout_telemetry import record_detail

# The post-stop phases this arm may act on. Deliberately narrower than the OS
# set (`ops/hold_recovery.RECOVERABLE_PHASES` also takes `starting`): a start
# already in flight is another actor's work, and racing it is the double-start
# this gate exists to prevent.
_RECOVERABLE_PHASES = frozenset({"stopping", "stopped"})

_TELEMETRY_GROUP = "updater_stop_recovery"

# The stop failure this arm is reacting to stays the leg's verdict; the line
# only names what happens next.
_DELEGATED = (
    "deferring to the stranded-hold completion (one bounded attempt), the "
    "watchdog's respawn, or a manual `ava start`; the stop rc stands"
)


def _recovery_enabled() -> bool:
    """`AVA_STOP_INCOMPLETE_RECOVERY` as a call-time read -- the arm's kill-switch."""
    from shared.config import settings

    return settings.gateway.stop_incomplete_recovery


def _attempt_timeout_s() -> float:
    """The bounded attempt's deadline (`AVA_STOP_INCOMPLETE_RECOVERY_TIMEOUT_SECONDS`)."""
    from shared.config import settings

    return settings.gateway.stop_incomplete_recovery_timeout_seconds


def capture_stop_episode() -> tuple[str, datetime] | None:
    """The maintenance generation this leg is stopping under, read just before it stops.

    None when no hold is readable -- the recovery gate then declines and the
    stop's own rc stands. Never raises: a pre-stop read must not be able to
    break the stop ladder it observes.
    """
    from shared import maintenance

    try:
        current = maintenance.snapshot()
    except Exception:
        return None
    if current is None or current.maintenance is None:
        return None
    if current.holder is None or current.acquired_at is None:
        return None
    return (current.holder, current.acquired_at)


def _skip(slug: str, detail: str) -> bool:
    """A gate refused before any attempt: verdict line + telemetry, no start."""
    print(f"[updater] stop-recovery: skipped -- {detail}; {_DELEGATED}", file=sys.stderr)
    record_detail(_TELEMETRY_GROUP, f"skipped-{slug}", 1.0)
    return False


def _fail(slug: str, detail: str) -> bool:
    """The one attempt ran and did not restore service: same shape, failed-*."""
    print(
        f"[updater] stop-recovery: bounded start failed -- {detail}; {_DELEGATED}",
        file=sys.stderr,
    )
    record_detail(_TELEMETRY_GROUP, f"failed-{slug}", 1.0)
    return False


def recover_incomplete_stop(
    repo: Path,
    ava_bin: Path,
    *,
    stop_rc: int,
    episode: tuple[str, datetime] | None,
    handoff_generation: str | None,
) -> bool:
    """Spend the one bounded attempt when the host is provably ours to recover.

    Called at the leg's `stop_rc != 0` exit. True when the host is back
    serving (the caller continues as success); False on every refusal or
    failure, with one verdict line already printed (plus a best-effort
    telemetry detail when a collector is active).
    Never raises: the leg must be able to return its own stop rc.
    """
    if not _recovery_enabled():
        # The switch's off state is the pre-#3942 behaviour: silent, so the
        # leg's rc is the whole verdict.
        return False
    try:
        return _gated_attempt(
            repo,
            ava_bin,
            stop_rc=stop_rc,
            episode=episode,
            handoff_generation=handoff_generation,
        )
    except Exception as exc:
        return _fail("unexpected", f"the caller-side recovery raised {exc!r}")


def _gated_attempt(
    repo: Path,
    ava_bin: Path,
    *,
    stop_rc: int,
    episode: tuple[str, datetime] | None,
    handoff_generation: str | None,
) -> bool:
    """The four gates, then the single bounded start attempt."""
    from shared import maintenance, updater_handoff

    if episode is None:
        return _skip("no-hold", "no maintenance generation was readable before the stop")
    try:
        current = maintenance.snapshot()
    except Exception as exc:
        return _skip("hold-unreadable", f"cannot read the maintenance hold ({exc!r})")
    if current is None or current.maintenance is None:
        return _skip("no-hold", "this unit no longer carries a maintenance hold")
    if not current.matches(*episode):
        return _skip(
            "foreign-hold", "the hold is a different generation than the one this leg stopped under"
        )
    phase = current.maintenance.phase
    if phase not in _RECOVERABLE_PHASES:
        return _skip(f"phase-{phase}", f"hold phase {phase!r} is not a post-stop completion phase")

    if handoff_generation is None:
        return _skip("handoff-absent", "this leg owns no updater handoff generation")
    try:
        handoff = updater_handoff.read()
    except Exception as exc:
        return _skip("handoff-absent", f"cannot read the updater handoff ({exc!r})")
    if handoff.status != "running" or handoff.generation != handoff_generation:
        return _skip(
            "handoff-absent",
            f"the updater handoff is {handoff.status!r} for generation {handoff.generation!r}",
        )
    try:
        owner_live = updater_handoff.owner_is_live(handoff)
    except Exception as exc:
        return _skip("handoff-absent", f"cannot read the updater handoff owner ({exc!r})")
    if not owner_live:
        return _skip("handoff-dead", "the updater handoff's owner process is gone")

    from shared import host_deploy_state

    try:
        state = host_deploy_state.read()
    except Exception as exc:
        return _skip("state-unreadable", f"cannot read the host deploy state ({exc!r})")
    if state is not None and (
        state.stranded_hold_since is not None or state.stranded_hold_attempts > 0
    ):
        return _skip(
            "stranded",
            "the host is already a declared stranded hold -- its own bounded completion owns "
            "the next attempt",
        )

    print(
        f"[updater] stop-recovery: the stop exited rc={stop_rc}; attempting one bounded "
        f"internal start"
    )
    timeout_s = _attempt_timeout_s()
    start_env = os.environ.copy()
    for key in ("AVA_CONFIG_FETCH", "AVA_CONFIG_SOURCE"):
        start_env.pop(key, None)
    try:
        completed = subprocess.run(
            [str(ava_bin), "start", "--persist-services", "--updater-telemetry"],
            cwd=repo,
            env=start_env,
            timeout=timeout_s,
            check=False,
        )
    except TimeoutExpired:
        return _fail("timeout", f"the bounded start did not finish within {timeout_s:g}s")
    except OSError as exc:
        return _fail("launch-failed", f"{ava_bin} failed to launch ({exc})")
    if completed.returncode != 0:
        return _fail(
            f"start-rc-{completed.returncode}", f"the internal start exited {completed.returncode}"
        )

    try:
        after = host_deploy_state.read()
    except Exception as exc:
        return _fail("not-serving", f"cannot read the posture row to confirm service ({exc!r})")
    posture = after.posture if after is not None else None
    if posture != host_deploy_state.POSTURE_IDLE:
        return _fail("not-serving", f"the posture row reads {posture!r}, not 'idle'")
    print(
        "[updater] stop-recovery: recovered the half-stopped host (bounded internal start) -- "
        "services serving, hold released"
    )
    record_detail(_TELEMETRY_GROUP, "recovered", 1.0)
    return True
