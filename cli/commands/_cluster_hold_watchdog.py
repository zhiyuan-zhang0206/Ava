"""`ava cluster hold-watchdog` — complete an orphaned maintenance hold, once (task #3887).

The command the OS scheduler runs (see ``shared.os_hold_watchdog`` for the job
and ``shared.hold_watchdog`` for the verdict). The 2026-09-17 S3 blackout was
110 minutes of a maintenance hold nobody owned; this command is the OS-side
actor that unbinds such a hold, bounded to ONE completion attempt per hold
generation.

Flow: evaluate (local evidence only — no database read) → when the verdict is
eligible, reserve this generation's attempt in the local file CAS → re-check
the gates → run the official completion ladder in-process → record the
outcome. No spawn channel is used on purpose: the blackout shape left exactly
one live layer (the platform scheduler), so the job process itself is the
executor, and the ladder's `ava start` is what brings the rest of the stack —
database included — back.

The ladder reuses ``cli.commands._hold_recover``'s legs — the same functions
#3142's bounded completion runs — so both mechanisms execute ONE definition of
the official stop/start/resume recipe. Each leg re-verifies the hold
generation under its own locks, and a hold released mid-ladder aborts it.

Semantics split (task #6294): an abort caused by the hold being released or
replaced while the attempt was in flight records "aborted (rescued within the
window)" — never a completion; an attempt that ran because the bound expired
records "expired-complete". The two wordings are distinct so drill readings
cannot confuse them.

Output lands on stderr (the channel the OS job captures into
``$AVA_HOME/logs/hold-watchdog.log``) and in a per-attempt
``$AVA_HOME/logs/hold-watchdog-<epoch>.log`` — the retention family an
operator already knows from the hold-recover logs.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from shared.log import logger


def _report(level: Literal["info", "warning", "error"], message: str) -> None:
    """Write one watchdog line where the scheduler's reader will find it.

    Same channel discipline as the watchdog probe: the job runs headless, so
    ``print`` to stderr — captured by launchd's StandardErrorPath / the
    crontab redirect — is the durable record, and the loguru call keeps the
    line visible when a human runs the command interactively.
    """
    if level == "error":
        logger.error(message)
    elif level == "warning":
        logger.warning(message)
    else:
        logger.info(message)
    print(message, file=sys.stderr)


def _new_attempt_log() -> Path:
    """A fresh ``$AVA_HOME/logs/hold-watchdog-<epoch>.log``, old siblings trimmed.

    Shares the updater family's log directory and retention
    (``ops.cluster_deploy._new_update_log``), the same family the #3142
    completion logs live in.
    """
    from ops.cluster_deploy import _new_update_log

    return _new_update_log("hold-watchdog")


def _log_line(path: Path, message: str) -> None:
    """Append one timestamped line to the attempt log; never raises."""
    try:
        with path.open("a") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError as exc:
        _report("warning", f"[hold-watchdog] attempt log write failed: {exc!r}")


def _run_ladder(
    phase: str, holder: str, acquired_at: datetime, *, attempt_log: Path
) -> tuple[str, int]:
    """Run the official completion ladder for ``phase``; return (note, rc).

    Reuses ``cli.commands._hold_recover``'s leg helpers — one definition of
    the recipe for both bounded-completion mechanisms. Every step is recorded
    before it runs, so a crash mid-ladder still names the step it died in.
    """
    from cli.commands import _hold_recover
    from shared import hold_watchdog

    steps = hold_watchdog.ladder_for_phase(phase)
    for step in steps:
        _log_line(attempt_log, f"[hold-watchdog] ladder step: {step}")
        _report("info", f"[hold-watchdog] ladder step: {step}")
        try:
            if step == "stop":
                _hold_recover._complete_stop()
            elif step == "start":
                _hold_recover._start_leg(holder, acquired_at)
            else:
                _hold_recover._resume_leg(holder, acquired_at)
        except Exception as exc:
            if _released_since(holder, acquired_at):
                note = f"aborted (rescued within the window): {step} refused: {exc!r}"[:500]
                _log_line(attempt_log, f"[hold-watchdog] {note}")
                _report("warning", f"[hold-watchdog] {note}")
            else:
                note = f"failed at {step}: {exc!r}"[:500]
                _log_line(attempt_log, f"[hold-watchdog] {note}")
                _report("error", f"[hold-watchdog] {note}")
            return note, 1
    note = f"expired-complete: ladder {' -> '.join(steps)} finished; hold released"
    return note, 0


def _released_since(holder: str, acquired_at: datetime) -> bool:
    """True when the exact generation the ladder was completing no longer stands.

    A leg failure takes one of two meanings (task #6294): while our generation
    stands, the leg FAILED; when a release (or replacement) swept the hold away
    mid-ladder, the attempt was rescued inside the window and is recorded with
    the abort wording, never as a completion. The split is decided on a fresh
    reading - never on the exception's wording - so it tracks state, not strings.
    """
    from shared import hold_watchdog

    current = hold_watchdog.evaluate()
    if current.kind is hold_watchdog.VerdictKind.NO_HOLD:
        return True
    if current.holder is None or current.acquired_at is None:
        return False
    return current.holder != holder or current.acquired_at != acquired_at


def _finish(episode: str, note: str, *, attempt_log: Path | None) -> None:
    """Record the attempt's outcome locally, plus best-effort DB mirroring.

    The local CAS outcome is the durable one (the mechanism must work with
    the database down); ``host_deploy_state.finish_stranded_recovery`` mirrors
    it onto the standing stranded-hold record when the ladder's start leg has
    brought the database back, and is skipped silently when it has not.
    """
    from shared import hold_watchdog

    if attempt_log is not None:
        _log_line(attempt_log, f"[hold-watchdog] outcome: {note}")
    try:
        hold_watchdog.finish_attempt(episode, note)
    except Exception as exc:
        _report("warning", f"[hold-watchdog] outcome not recorded in the attempt CAS: {exc!r}")
    try:
        from shared.host_deploy_state import finish_stranded_recovery

        finish_stranded_recovery(f"hold-watchdog: {note}"[:500])
    except Exception as exc:
        _report("info", f"[hold-watchdog] stranded-hold record not updated ({exc!r})")


def _run_watchdog() -> int:
    from shared import hold_watchdog

    verdict = hold_watchdog.evaluate()
    if verdict.kind is hold_watchdog.VerdictKind.NO_HOLD:
        return 0
    if verdict.kind is hold_watchdog.VerdictKind.BACK_OFF:
        # A standing hold the gates refuse to act on: reported so the wait is
        # visible, quiet for the common no-hold case above.
        _report("info", f"[hold-watchdog] standing down: {verdict.detail}")
        return 0

    episode = verdict.episode
    if episode is None:
        # An eligible verdict always carries a generation; this is a bug guard.
        _report("error", "[hold-watchdog] eligible verdict without a hold generation")
        return 1
    attempt = hold_watchdog.reserve_attempt(
        episode,
        max_attempts=hold_watchdog.MAX_ATTEMPTS,
        cooldown_s=hold_watchdog.cooldown_seconds(),
    )
    if attempt is None:
        _report(
            "info",
            f"[hold-watchdog] {verdict.detail}; this generation's attempt budget is spent "
            "(one bounded completion per hold generation)",
        )
        return 0

    attempt_log = _new_attempt_log()
    _report(
        "warning",
        f"[hold-watchdog] ORPHAN HOLD: {verdict.detail} "
        f"(holder={verdict.holder}, phase={verdict.phase}); spending attempt #{attempt} "
        f"of {hold_watchdog.MAX_ATTEMPTS}; log {attempt_log}",
    )
    _log_line(attempt_log, f"[hold-watchdog] attempt #{attempt} for {episode}: {verdict.detail}")

    # Re-check immediately before acting: the reservation is not a claim about
    # the present, and a hold released between evaluation and here must abort
    # with the abort wording, not run a ladder against a replaced generation.
    recheck = hold_watchdog.evaluate()
    if recheck.kind is not hold_watchdog.VerdictKind.ELIGIBLE or recheck.episode != episode:
        replaced = recheck.episode is not None and recheck.episode != episode
        if recheck.kind is hold_watchdog.VerdictKind.NO_HOLD or replaced:
            note = f"aborted (rescued within the window): {recheck.code}: {recheck.detail}"
        else:
            note = f"aborted before the ladder: {recheck.code}: {recheck.detail}"
        _report("warning", f"[hold-watchdog] {note}; attempt spent")
        _finish(episode, note, attempt_log=attempt_log)
        return 0

    acquired_at = verdict.acquired_at
    if acquired_at is None:
        # An eligible verdict always carries a full generation; a None here is
        # the same class of bug as the missing episode above.
        _report("error", "[hold-watchdog] eligible verdict without an acquisition instant")
        return 1
    note, rc = _run_ladder(
        recheck.phase or verdict.phase or "",
        verdict.holder or "",
        acquired_at,
        attempt_log=attempt_log,
    )
    _finish(episode, note, attempt_log=attempt_log)
    _report("info" if rc == 0 else "error", f"[hold-watchdog] outcome: {note}")
    return rc


def cmd_hold_watchdog() -> int:
    """Complete this home's orphaned maintenance hold, once, when provably orphaned.

    Returns 0 for the quiet readings (no hold, a gate that defers, the attempt
    budget already spent, an aborted attempt), 1 only when a spent attempt
    failed — the scheduler discards the exit code either way; the log line is
    the record. Unexpected failures are reported rather than traced into the
    job log raw, so every exit names its cause.
    """
    try:
        return _run_watchdog()
    except (
        Exception
    ) as exc:  # fail-fast-ok: a scheduled one-shot reports and exits, never crashes raw
        _report("error", f"[hold-watchdog] unexpected failure: {exc!r}")
        return 1


def cmd_hold_watchdog_register() -> int:
    """Register the OS-scheduled hold watchdog (manual / debug entry)."""
    from shared.os_hold_watchdog import register_hold_watchdog

    try:
        register_hold_watchdog()
    except RuntimeError as e:
        print(f"  * {e}", file=sys.stderr)
        return 1
    return 0


def cmd_hold_watchdog_unregister() -> int:
    """Remove the OS-scheduled hold watchdog (manual / debug entry)."""
    from shared.os_hold_watchdog import unregister_hold_watchdog

    unregister_hold_watchdog()
    return 0
