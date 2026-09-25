"""Bounded automatic completion of an orphaned maintenance hold (task #3887).

The 2026-09-17 S3 blackout (110 minutes; task #3719) was, at the mechanism
level, a maintenance hold nobody owned any more: the stop leg's driver was
cancelled mid-transition, the hold then kept the host stopped, and every
automatic path was either shape-gated away (#3142 completes only update-armed
holds and never from the gateway capability's watchdog round; the pre-stop
release never applies to a started stop) or itself lived inside the stopped
stack. The user ruling that followed (agent #405, 2026-09-17 23:38): an
unclaimed pause must unbind itself - a bug, not a policy choice.

This module provides local completion eligibility and a compare-and-set attempt
budget for the external transition executor. It never launches processes or
registers an OS job. The verdict uses files, locks, and captured process identity
because the application stack and database may both be stopped.

**Gate list (all conditions required; first failure decides the reading).**

1. A maintenance hold stands (``pause_owner`` journal, local file) and its
   phase is one of ``RECOVERABLE_PHASES`` (post-stop: stopping / stopped /
   starting). Anything else is not this mechanism's business.
2. The hold carries no failed receipts. Repair precedes completion; an
   automatic actor must never clear failed work.
3. The recovery switch is on (``settings.gateway.stranded_hold_recovery``;
   see ``enabled()`` for the unreadable-default reasoning).
4. The recorded shepherd is DEAD - and only dead. Missing (a pre-#3270
   journal) or unreadable identity is missing evidence, and missing evidence
   is never a completion license (task #3270's discipline). A live shepherd
   is someone still working.
5. Nothing is executing under the hold, by local evidence: no live updater
   handoff, no live orchestration session (updater / rollout /
   cluster-restart / hold-recover), and the updater mutex not held. The
   hold-recover session is included so this mechanism cannot race #3142's
   own completion session.
6. The lifecycle lock is free: any local start/stop (a boot autostart
   included) holds it for its whole run, and completing a hold while one is
   in flight is the double-master shape this check exists to prevent.
   A probe that cannot take the lock backs off WITHOUT spending the attempt.

Then the bound: the hold's age must have reached ``min_age`` (from its own
``acquired_at``), or a declared intended lifetime must have expired - the
forward-compat hook for #3724 §2.1 (see ``_intended_expiry``). The bound is
the 6294 constraint: completion is led by the intended life when one is
declared, with the 30-minute floor as the default.

**The budget.** ``MAX_ATTEMPTS`` attempts per hold generation, plus a
cooldown (``cooldown_seconds``), in a local file CAS
(``$AVA_HOME/state/hold-watchdog-attempt``). A new generation resets the
budget; a spent budget is never refunded - the same shape as #3142's
``host_deploy_state.reserve_stranded_recovery`` (one attempt, 900s cooldown),
so the two mechanisms read alike. The CAS file is tiny JSON written
atomically under a sibling lock; an unreadable CAS file reads as "do not act"
(the bound must not fail open) and is logged loudly.

**Semantics split (task #6294 constraint 1).** A hold released externally
while an attempt is in flight is "rescued within the window": the attempt
aborts and records an abort note, never a completion. An attempt that runs
because the bound expired completes normally and records "expired-complete".
The two outcomes carry distinct wording so the drill readings
(S5/S7) cannot confuse them.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from loguru import logger

# The post-stop phases a completion may act on. Mirrors
# `ops.hold_recovery.RECOVERABLE_PHASES` (locked equal by tests): pre-stop
# phases are deliberately absent because an incomplete drain belongs to
# `resume --cancel`, never to an automatic stop.
RECOVERABLE_PHASES = frozenset({"stopping", "stopped", "starting"})

#: One attempt per hold generation — the mechanism's qualitative bound. The
#: ruling (task #3887): an orphaned hold gets ONE bounded shot at the official
#: ladder; retrying a state a retry cannot fix is how a watchdog becomes the
#: outage. A new hold generation (new holder/acquired_at) starts a fresh
#: budget; a spent budget is never refunded.
MAX_ATTEMPTS = 1

#: Compiled fallbacks for the two config knobs. Unreadable config must not
#: silently change behavior in a full-stop shape, and these are the same
#: values the Settings fields default to (locked equal by tests). The floor
#: reuses the family's 30-minute window (task #3270's AUTO_RELEASE_S); the
#: cooldown mirrors #3142's own 900s.
DEFAULT_MIN_AGE_SECONDS = 1800.0
DEFAULT_COOLDOWN_SECONDS = 900.0

#: Lock-probe budget. Matches the lifecycle lock's own contention timeout in
#: `cli.commands._pause_resume.exclusive_resources`: a holder past this point
#: is doing work we must not walk into.
_PROBE_LOCK_TIMEOUT_S = 0.1

_ATTEMPT_NAME = "hold-watchdog-attempt"

#: The forward-compat key for a hold's declared intended lifetime (#3724
#: §2.1, not implemented yet). Read tolerantly from the raw journal; absent
#: or malformed reads as absent, and the floor governs.
_INTENDED_EXPIRY_KEY = "intended_expires_at"

#: The local orchestration sessions whose liveness means "something is still
#: executing". `hold-recover` is #3142's completion session: both mechanisms
#: complete the same ladder, so one must stand down while the other runs.
_ORCHESTRATION_SERVICES = ("updater", "rollout", "cluster-restart", "hold-recover")

#: Sentinal returned by `_live_orchestration_session` when the session backend
#: cannot be probed at all: a reading that must defer, never a session name.
_SESSION_PROBE_UNREADABLE = "session probe unreadable"


class VerdictKind(StrEnum):
    """The three readings of the completion question."""

    ELIGIBLE = "eligible"
    NO_HOLD = "no-hold"
    BACK_OFF = "back-off"


@dataclass(frozen=True)
class HoldWatchdogVerdict:
    """One round's reading. `code` is the stable token tests assert on;
    `detail` is the operator-facing sentence. `episode` names the hold
    generation the reading belongs to."""

    kind: VerdictKind
    code: str
    detail: str = ""
    holder: str | None = None
    acquired_at: datetime | None = None
    phase: str | None = None
    age_s: float | None = None
    due_in_s: float | None = None

    @property
    def episode(self) -> str | None:
        """The hold generation this reading belongs to, as `holder|acquired_at`."""
        if self.holder is None or self.acquired_at is None:
            return None
        return episode_key(self.holder, self.acquired_at)


def episode_key(holder: str, acquired_at: datetime) -> str:
    """The stable generation key the attempt CAS records and compares."""
    return f"{holder}|{acquired_at.isoformat()}"


# --- local evidence probes ---------------------------------------------------


def enabled() -> bool:
    """The mechanism's kill-switch: ``settings.gateway.stranded_hold_recovery``.

    The field is resolvable on the settings-lite boot path (it is a
    ``LITE_FIELDS`` row), so an OS-scheduled one-shot resolves it without the
    database or a gateway fetch - that is the whole point of the row. Reads
    resolve pending > env/.env > default, so an operator can flip the switch
    from the unit's ``.env`` (or the process environment) and the next job
    run honors it.

    Unreadable resolves ON: the field's default is True and the mechanism is
    the fix for a bug-shaped blackout (user ruling, task #3887) - a config
    read failure must not silently restore the blackout.
    """
    from shared.config import settings

    try:
        return bool(settings.gateway.stranded_hold_recovery)
    except Exception:  # fail-fast-ok: full-stop recovery must not depend on a readable config
        logger.warning("[hold-watchdog] cannot resolve the recovery switch; assuming ON")
        return True


def min_age_seconds() -> float:
    """The hold-age floor before completion (config; 30 minutes by default)."""
    from shared.config import settings

    try:
        return float(settings.gateway.hold_watchdog_min_age_seconds)
    except Exception:  # fail-fast-ok: see enabled()
        logger.warning(
            "[hold-watchdog] cannot resolve the age floor; using {}s", DEFAULT_MIN_AGE_SECONDS
        )
        return DEFAULT_MIN_AGE_SECONDS


def cooldown_seconds() -> float:
    """The cooldown between two attempts of one generation (config; 900s default)."""
    from shared.config import settings

    try:
        return float(settings.gateway.hold_watchdog_cooldown_seconds)
    except Exception:  # fail-fast-ok: see enabled()
        logger.warning(
            "[hold-watchdog] cannot resolve the cooldown; using {}s", DEFAULT_COOLDOWN_SECONDS
        )
        return DEFAULT_COOLDOWN_SECONDS


def _executing_block() -> tuple[str, str] | None:
    """The first local signal that something is still executing, or None.

    Every probe here is local by design (module docstring). A probe that
    cannot answer counts as executing: missing evidence must not license the
    completion.
    """
    from shared import updater_handoff

    handoff = updater_handoff.read()
    if handoff.status == "invalid":
        return ("handoff-unreadable", "the updater handoff journal is unreadable")
    if handoff.status == "pending" and not handoff.expired:
        return ("handoff-pending", f"updater handoff {handoff.generation} is pending")
    if handoff.status == "running" and _handoff_owner_alive(handoff):
        return ("handoff-running", f"updater handoff {handoff.generation} has a live process owner")
    session = _live_orchestration_session()
    if session == _SESSION_PROBE_UNREADABLE:
        return ("orchestration-session", "orchestration session liveness is unreadable")
    if session is not None:
        return ("orchestration-session", f"orchestration session {session} is in flight")
    if _updater_lock_held():
        return ("updater-lock", "a local updater process holds the updater lock")
    return None


def _handoff_owner_alive(handoff: object) -> bool:
    """Fail-closed owner liveness: an unjudgeable owner counts as alive."""
    from shared import updater_handoff

    try:
        return updater_handoff.owner_is_live(handoff)  # type: ignore[arg-type]
    except Exception:
        logger.warning("[hold-watchdog] cannot judge the updater handoff owner; deferring")
        return True


def _live_orchestration_session() -> str | None:
    """The first live orchestration session on this host, or None.

    Local process/session-record reads only (the session backend is a local
    supervisor); a backend that cannot answer returns a placeholder that
    reads as executing, so an unreadable probe defers the completion.
    """
    from shared.cluster import session_name
    from shared.session_backend import get_backend

    try:
        backend = get_backend()
        for service in _ORCHESTRATION_SERVICES:
            session = session_name(service)
            if backend.has_session(session):
                return session
    except Exception as exc:
        logger.warning("[hold-watchdog] cannot probe orchestration sessions ({}); deferring", exc)
        return _SESSION_PROBE_UNREADABLE
    return None


def _updater_lock_held() -> bool:
    """Whether a local updater currently holds the updater mutex.

    The updater lock is the host's authoritative "an updater process is alive"
    signal that does not depend on any journal being written; probing it is a
    non-blocking acquire-and-release, exactly as a second updater would test
    it. A missing lock file is decidable ("no updater holds it"); an
    unreadable one defers.
    """
    from shared.host_deploy_state import (
        _updater_lock_path,  # the filename's one home is the updater module
        release_updater_lock,
        try_acquire_updater_lock,
    )

    try:
        if not _updater_lock_path().exists():
            return False
        acquired = try_acquire_updater_lock()
    except OSError as exc:
        logger.warning("[hold-watchdog] cannot probe the updater lock ({}); deferring", exc)
        return True
    if acquired:
        release_updater_lock()
        return False
    return True


def _lifecycle_busy() -> bool:
    """Whether a whole local start/stop operation currently holds the lifecycle lock.

    This is the boot/act mutual exclusion (constraint: never double-master).
    `cli.commands._pause_resume.exclusive_resources` holds this lock for the
    entire duration of every local start/stop - a boot autostart's ``ava
    start`` included - so holding it means an actor that owns the transition
    is already running. The probe takes and immediately releases the lock; a
    probe that cannot answer defers (unreadable evidence is not a license).
    """
    from shared.platform import LockTimeoutError, file_lock
    from shared.ui_update_state import lifecycle_lock_path

    try:
        with file_lock(lifecycle_lock_path(), timeout_s=_PROBE_LOCK_TIMEOUT_S):
            return False
    except LockTimeoutError:
        return True
    except OSError as exc:
        logger.warning("[hold-watchdog] cannot probe the lifecycle lock ({}); deferring", exc)
        return True


def _intended_expiry() -> float | None:
    """The hold's declared intended-lifetime expiry, as a Unix timestamp.

    Forward-compat hook for #3724 §2.1's "expected maximum lifetime": the
    future producer will stamp an ISO-8601 ``intended_expires_at`` inside the
    journal's ``maintenance`` payload (an unknown key today's decoders
    ignore). Absent or malformed reads as absent - this is a bound refinement,
    never a safety signal - and the age floor governs.
    """
    from shared import pause_owner

    try:
        raw = json.loads(pause_owner.state_path().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    raw = cast("dict[str, object]", raw)
    maintenance = raw.get("maintenance")
    if not isinstance(maintenance, dict):
        return None
    payload = cast("dict[str, object]", maintenance)
    value = payload.get(_INTENDED_EXPIRY_KEY)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


# --- the verdict -------------------------------------------------------------


def evaluate(*, now: float | None = None) -> HoldWatchdogVerdict:
    """This host's completion reading, from local evidence only.

    The gate order matches the module docstring; the first failing gate
    decides `code`/`detail`. `now` overrides the clock (tests).
    """
    from shared import pause_owner
    from shared.hold_driver import liveness

    clock = time.time() if now is None else now
    try:
        current = pause_owner.read()
    except OSError as exc:
        return _back("hold-unreadable", f"cannot read the pause owner journal: {exc!r}")
    if current.status == "invalid":
        return _back("hold-unreadable", "the pause owner journal is unreadable")
    if current.status != "paused" or current.maintenance is None:
        return _no_hold()
    holder, acquired_at = current.holder, current.acquired_at
    if holder is None or acquired_at is None:
        return _back("hold-incomplete", "the hold carries no (holder, acquired_at) generation")
    phase = current.maintenance.phase
    generation = {"holder": holder, "acquired_at": acquired_at, "phase": phase}
    if phase not in RECOVERABLE_PHASES:
        return _back(
            "phase",
            f"hold phase {phase!r} is not a post-stop completion phase",
            **generation,
        )
    unsettled = current.maintenance.unsettled_failures()
    if unsettled:
        return _back(
            "failures",
            f"the hold carries {len(unsettled)} failed receipt(s); repair precedes any completion",
            **generation,
        )
    if not enabled():
        return _back("disabled", "the stranded-hold recovery switch is off", **generation)
    driver = liveness(current.driver)
    if driver != "dead":
        return _back(
            f"driver-{driver}",
            f"the hold's shepherding process is {driver}, not provably gone",
            **generation,
        )
    blocking = _executing_block()
    if blocking is not None:
        code, detail = blocking
        return _back(code, detail, **generation)
    if _lifecycle_busy():
        return _back(
            "lifecycle-busy",
            "a local start/stop operation is in progress (boot or operator action)",
            **generation,
        )
    age = clock - acquired_at.timestamp()
    intended = _intended_expiry()
    due = intended if intended is not None else acquired_at.timestamp() + min_age_seconds()
    if clock < due:
        return _back(
            "young",
            f"hold is {age:.0f}s old; its completion bound is {due - clock:.0f}s away"
            + (" (intended lifetime)" if intended is not None else ""),
            age_s=age,
            due_in_s=due - clock,
            **generation,
        )
    return HoldWatchdogVerdict(
        kind=VerdictKind.ELIGIBLE,
        code="due",
        detail=(
            f"hold has no live owner and nothing executes under it; "
            f"age {age:.0f}s has reached its bound"
        ),
        age_s=age,
        due_in_s=due - clock,
        holder=holder,
        acquired_at=acquired_at,
        phase=phase,
    )


def _back(code: str, detail: str, **kwargs: object) -> HoldWatchdogVerdict:
    return HoldWatchdogVerdict(kind=VerdictKind.BACK_OFF, code=code, detail=detail, **kwargs)  # type: ignore[arg-type]


def _no_hold() -> HoldWatchdogVerdict:
    return HoldWatchdogVerdict(kind=VerdictKind.NO_HOLD, code="no-hold")


def ladder_for_phase(phase: str) -> tuple[str, ...]:
    """The official completion ladder for one post-stop phase.

    ``stopping`` -> complete the stop, then start and resume; ``stopped`` /
    ``starting`` -> start and resume. Mirrors the #3142 recipe
    (``cli.commands._hold_recover``); the CLI maps these step names onto the
    shared leg functions so both mechanisms run one ladder definition.
    """
    if phase == "stopping":
        return ("stop", "start", "resume")
    if phase in ("stopped", "starting"):
        return ("start", "resume")
    raise ValueError(f"phase {phase!r} has no completion ladder")


# --- the per-episode attempt CAS --------------------------------------------


class AttemptStateUnreadableError(RuntimeError):
    """The attempt CAS file exists but cannot be parsed."""


@dataclass(frozen=True)
class AttemptState:
    """The recorded budget state of one hold generation."""

    episode: str | None = None
    attempts: int = 0
    attempted_at: float | None = None
    note: str | None = None
    ts: float | None = None


def attempt_path() -> Path:
    """The attempt CAS file: ``$AVA_HOME/state/hold-watchdog-attempt``."""
    import shared.paths

    return shared.paths.ava_home() / "state" / _ATTEMPT_NAME


def attempt_lock_path() -> Path:
    """The advisory lock serializing CAS reads/writes."""
    return attempt_path().with_name(_ATTEMPT_NAME + ".lock")


def _read_attempt_unlocked(path: Path) -> AttemptState | None:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise AttemptStateUnreadableError(f"attempt CAS is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise AttemptStateUnreadableError("attempt CAS root must be an object")
    raw = cast("dict[str, object]", raw)
    episode = raw.get("episode")
    attempts = raw.get("attempts", 0)
    attempted_at = raw.get("attempted_at")
    note = raw.get("note")
    ts = raw.get("ts")
    if episode is not None and not isinstance(episode, str):
        raise AttemptStateUnreadableError("attempt CAS episode must be a string or null")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise AttemptStateUnreadableError("attempt CAS attempts must be a nonnegative integer")
    if attempted_at is not None and (
        isinstance(attempted_at, bool) or not isinstance(attempted_at, (int, float))
    ):
        raise AttemptStateUnreadableError("attempt CAS attempted_at must be a number or null")
    if note is not None and not isinstance(note, str):
        raise AttemptStateUnreadableError("attempt CAS note must be a string or null")
    if ts is not None and (isinstance(ts, bool) or not isinstance(ts, (int, float))):
        raise AttemptStateUnreadableError("attempt CAS ts must be a number or null")
    return AttemptState(
        episode=episode,
        attempts=attempts,
        attempted_at=None if attempted_at is None else float(attempted_at),
        note=note,
        ts=None if ts is None else float(ts),
    )


def read_attempt() -> AttemptState | None:
    """Read the attempt CAS as it stands (None when absent); raises when corrupt."""
    return _read_attempt_unlocked(attempt_path())


def _write_attempt(state: AttemptState) -> None:
    path = attempt_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "episode": state.episode,
        "attempts": state.attempts,
        "attempted_at": state.attempted_at,
        "note": state.note,
        "ts": state.ts,
    }
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=".hold-watchdog-attempt-", suffix=".tmp")
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)  # noqa: PTH105 — explicit atomic replace injection seam
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def reserve_attempt(
    episode: str, *, max_attempts: int, cooldown_s: float, now: float | None = None
) -> int | None:
    """Reserve this generation's next attempt, or None when it may not act.

    The compare-and-set mirrors ``host_deploy_state.reserve_stranded_recovery``:
    a different generation resets the budget, attempts below ``max_attempts``
    and (when a prior attempt exists) ``cooldown_s`` elapsed are required, and
    the reservation is atomic under the sibling lock. An unreadable CAS file
    returns None: the bound must never fail open, so the mechanism stands
    down (loudly) until a human resolves the file.
    """
    from shared.platform import LockTimeoutError, file_lock

    clock = time.time() if now is None else now
    try:
        with file_lock(attempt_lock_path(), timeout_s=_PROBE_LOCK_TIMEOUT_S):
            try:
                state = _read_attempt_unlocked(attempt_path())
            except AttemptStateUnreadableError as exc:
                logger.warning("[hold-watchdog] attempt CAS unreadable; standing down: {}", exc)
                return None
            same_episode = state is not None and state.episode == episode
            attempts = state.attempts if same_episode and state is not None else 0
            last = state.attempted_at if same_episode and state is not None else None
            if attempts >= max_attempts:
                logger.info(
                    "[hold-watchdog] episode {} already spent its attempt budget ({}/{})",
                    episode,
                    attempts,
                    max_attempts,
                )
                return None
            if last is not None and clock - last < cooldown_s:
                logger.info(
                    "[hold-watchdog] episode {} is inside its cooldown ({:.0f}s of {:.0f}s)",
                    episode,
                    clock - last,
                    cooldown_s,
                )
                return None
            _write_attempt(
                AttemptState(episode=episode, attempts=attempts + 1, attempted_at=clock, ts=clock)
            )
            return attempts + 1
    except LockTimeoutError:
        logger.warning("[hold-watchdog] attempt CAS lock is contended; standing down")
        return None


def finish_attempt(episode: str, note: str, *, now: float | None = None) -> bool:
    """Record an attempt's outcome; True when it landed on the live generation.

    Deliberately attempts-preserving (the budget was spent at reservation and
    is never refunded) and a no-op when the generation was replaced between
    reservation and outcome — the episode is over, its note is moot. Raises
    on an unreadable CAS file; callers record best-effort.
    """
    from shared.platform import file_lock

    clock = time.time() if now is None else now
    with file_lock(attempt_lock_path(), timeout_s=_PROBE_LOCK_TIMEOUT_S):
        state = _read_attempt_unlocked(attempt_path())
        if state is None or state.episode != episode:
            return False
        _write_attempt(
            AttemptState(
                episode=state.episode,
                attempts=state.attempts,
                attempted_at=state.attempted_at,
                note=note,
                ts=clock,
            )
        )
        return True
