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

Before spending the attempt the command settles the completion-environment
question (`_completion_environment_problem`, task #4080): only a pure
agent-runner's start leg builds its OTLP relay from the gateway's published
`AVA_GATEWAY_OTLP_ENDPOINT`, and the 2026-09-19 gateway-migration window burned
a generation's single attempt on a relay nothing could have built. A not-ready
environment stands down with the attempt UNSET; the question is re-asked every
scheduled run, so the attempt waits for the window that can complete.

The ladder reuses ``cli.commands._hold_recover``'s legs — the same functions
#3142's bounded completion runs — so both mechanisms execute ONE definition of
the official stop/start/resume recipe. Each leg re-verifies the hold
generation under its own locks, and a hold released mid-ladder aborts it.

Semantics split (task #6294): an abort caused by the hold being released or
replaced while the attempt was in flight records "aborted (rescued within the
window)" — never a completion; an attempt that ran because the bound expired
records "expired-complete". The two wordings are distinct so drill readings
cannot confuse them.

The outcome reaches the attempt log unconditionally; the fleet record
(`host_deploy_state`) is written best-effort — a settings-lite context (this
job on a pure runner) cannot dial the database at all, so a note that cannot
land is queued durably and backfilled by the first DB-capable run (task #4080).

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


def _completion_environment_problem() -> str | None:
    """Why the completion environment is not ready, or None when it is.

    The pre-attempt gate (task #4080). Only a PURE agent-runner's converge
    builds the gateway OTLP relay (`cli.commands._otel_collector._otlp_exporters`),
    and that build is the precondition the 2026-09-19 migration window failed:
    `AVA_GATEWAY_OTLP_ENDPOINT` was not yet published, the start leg died at the
    relay step, and the generation's single attempt was spent in a window
    nothing could have completed. Every other capability set resolves its
    telemetry from its own config, so the gate has no question for it (None).

    For the pure runner the endpoint is resolved exactly like the start leg's
    own boot resolves its config (`shared.bootstrap.resolve_bootstrap_values`:
    fresh snapshot / live fetch / last-known-snapshot), so the reading matches
    what the start leg will see. A reading that cannot be made — an unresolved
    capability set included — defers: the budget must not fail open.
    """
    from cli.commands._repo import _roles_or_none

    roles = _roles_or_none()
    if roles is None:
        return "this unit's capability set cannot be resolved"
    if roles != frozenset({"agent-runner"}):
        return None
    from cli.commands._otel_collector import gateway_otlp_endpoint_problem
    from shared.bootstrap import BootstrapFetchError, resolve_bootstrap_values

    try:
        values = resolve_bootstrap_values()
    except BootstrapFetchError as exc:
        return str(exc).splitlines()[0].strip()
    endpoint = values.get("AVA_GATEWAY_OTLP_ENDPOINT", "")
    if not endpoint.strip():
        return "the gateway has not published AVA_GATEWAY_OTLP_ENDPOINT yet"
    return gateway_otlp_endpoint_problem(endpoint)


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
    """Record the attempt's outcome locally, plus best-effort fleet-record mirroring.

    The local CAS outcome is the durable one (the mechanism must work with the
    database down). The fleet record goes through
    ``host_deploy_state.record_stranded_recovery_note``: the write lands when
    the database is reachable from this process, and is otherwise QUEUED
    durably (task #4080 — the OS job runs settings-lite, where a pure runner's
    DB URL is the never-dialed placeholder, so the direct write can only ever
    fail there; a later DB-capable run backfills the note).
    """
    from shared import hold_watchdog

    if attempt_log is not None:
        _log_line(attempt_log, f"[hold-watchdog] outcome: {note}")
    try:
        hold_watchdog.finish_attempt(episode, note)
    except Exception as exc:
        _report("warning", f"[hold-watchdog] outcome not recorded in the attempt CAS: {exc!r}")
    try:
        from shared.host_deploy_state import record_stranded_recovery_note

        disposition = record_stranded_recovery_note(f"hold-watchdog: {note}"[:500])
    except Exception as exc:
        _report(
            "warning",
            f"[hold-watchdog] stranded-hold note could not be recorded or queued: {exc!r}",
        )
        return
    if disposition == "queued":
        _report(
            "info",
            "[hold-watchdog] fleet record not writable from this context; "
            "outcome queued for backfill",
        )


def _backfill_queued_note() -> None:
    """Land a note a previous run had to queue (task #4080); never raises.

    A settings-lite run cannot dial a pure runner's database, so an outcome may
    sit in the local queue until a DB-capable path runs — this run on a
    gateway-serving unit, a watchdog round on a pure runner. A failure just
    keeps the note queued; the queue-time report already carries the narrative.
    """
    from shared.host_deploy_state import flush_pending_stranded_recovery_note

    try:
        backfilled = flush_pending_stranded_recovery_note()
    except Exception:
        return
    if backfilled:
        _report("info", "[hold-watchdog] backfilled a queued stranded-hold note")


def _run_watchdog() -> int:
    from shared import hold_watchdog

    _backfill_queued_note()
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
    environment = _completion_environment_problem()
    if environment is not None:
        # Not-ready completion environment (task #4080): stand down with the
        # attempt UNSET — spending it here is how the 2026-09-19 window burned
        # the generation's only shot at a ladder nothing could have finished.
        _report(
            "warning",
            f"[hold-watchdog] deferred: completion environment not ready ({environment}); "
            "the ladder is withheld and this run spends no attempt",
        )
        return 0

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
