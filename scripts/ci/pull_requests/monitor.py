"""Read-only CI queries, polling and diagnostic output; never submits or re-runs work."""

from __future__ import annotations

import json
import sys
import time

from scripts.ci.pull_requests import status
from scripts.ci.pull_requests.status import (
    _LIMBO_AGE_SECONDS,
    MAX_CONSECUTIVE_ERRORS,
    CIResult,
    CIStatus,
    _pending_reason,
)


def query_once(pr: str, repo: str, *, as_json: bool) -> int:
    """One-shot probe: print the current verdict and exit (legacy contract —
    PENDING / NO_CHECKS / ERROR exit 0; FAILED / MERGE_CONFLICT /
    NO_WORKFLOW_RUNS / draft-gated NOT_READY exit 1)."""
    result = status.check_ci(pr, repo=repo)

    if as_json:
        print(
            json.dumps(
                {
                    "verdict": result.verdict.value,
                    "mergeable": result.mergeable,
                    "completed": result.completed,
                    "pending": result.pending,
                    "limbo": result.limbo,
                    "passed": result.passed,
                    "failed": result.failed,
                    "workflow_checks": result.workflow_checks,
                    "trunk_checks": result.trunk_checks,
                    "is_draft": result.is_draft,
                    "core_skipped": result.core_skipped,
                    "error_detail": result.error_detail,
                    "terminal": result.verdict.is_terminal,
                },
                indent=2,
            )
        )
    else:
        print(result.summary())

    return (
        0
        if result.verdict is CIStatus.ALL_PASSED
        else 1
        if result.verdict
        in (CIStatus.FAILED, CIStatus.MERGE_CONFLICT, CIStatus.NO_WORKFLOW_RUNS, CIStatus.NOT_READY)
        else 0
    )


def _deadline_hit(
    deadline: float | None, pr: str, timeout: int, what: str = "CI still pending"
) -> bool:
    """True once `deadline` passes — the --timeout bound for --wait use (which
    has no watchdog). Prints the reason so the completion notice carries it."""
    if deadline is None:
        return False
    if time.monotonic() >= deadline:
        print(f"PR #{pr} {what} after {timeout}s — timed out.", file=sys.stderr, flush=True)
        return True
    return False


def _report_limbo(result: CIResult, reported: tuple[int, ...] | None) -> tuple[int, ...] | None:
    """Name GitHub-limbo runs loudly on first sight (and on any change).
    Never silent (task #3275): this is the state that used to eat a --wait budget invisibly.
    Returns the new reported-state key so a repeat poll does not spam the same notice.
    """
    if not result.limbo:
        return reported
    key = tuple(sorted(r["id"] for r in result.limbo))
    if key == reported:
        return reported
    stuck = ", ".join(f"{r['name']} (#{r['id']}, {r['age_s'] // 60}m)" for r in result.limbo)
    print(
        f"[ci] GitHub limbo: {len(result.limbo)} run(s) for this head are queued with zero "
        f"jobs and no progress for >= {int(_LIMBO_AGE_SECONDS // 60)} minutes — they may "
        f"never complete without scheduler recovery (task #3275): {stuck}",
        file=sys.stderr,
        flush=True,
    )
    print(
        "[ci]   inspect with `gh run view <id>`; queued runs without executed jobs "
        "remain pending, never green",
        file=sys.stderr,
        flush=True,
    )
    return key


def _limbo_timeout_note(pr: str, result: CIResult) -> None:
    """Add limbo context to a --wait deadline message (task #3275)."""
    if not result.limbo:
        return
    ids = ", ".join(str(r["id"]) for r in result.limbo)
    print(
        f"PR #{pr} the block is {len(result.limbo)} GitHub-limbo run(s) ({ids}; queued, "
        "zero jobs, aged — task #3275)",
        file=sys.stderr,
        flush=True,
    )


def _conclude_settled(result: CIResult, pr: str) -> int:
    """Report a settled read-only wait verdict and return its exit code."""
    if result.verdict is CIStatus.ALL_PASSED:
        print(f"PR #{pr} CI green: {result.summary()}")
        return 0

    print(f"PR #{pr} CI NOT green: {result.summary()}", file=sys.stderr, flush=True)
    if result.failed:
        names = ", ".join(c.get("name", "?") for c in result.failed)
        print(f"PR #{pr} failed checks: {names}", file=sys.stderr, flush=True)
    if result.error_detail:
        print(f"PR #{pr} detail: {result.error_detail}", file=sys.stderr, flush=True)
    return 1


def wait_for_verdict(
    pr: str,
    repo: str,
    every: int,
    timeout: int,
) -> int:
    """Poll check_ci until the verdict settles, then report and exit.
    Never loops silently: a persistent gh/network failure exits 3 after MAX_CONSECUTIVE_ERRORS
    attempts with the error printed — the silent infinite loop this was built to eliminate
    (2026-08-02, PR #1243).

    GitHub-limbo runs (task #3275) are named loudly as soon as they are seen and again at the
    deadline. They remain pending because no jobs executed; diagnosis cannot replace evidence.
    """
    consecutive_errors = 0
    no_checks_reported = False
    deadline = time.monotonic() + timeout if timeout else None

    limbo_reported: tuple[int, ...] | None = None

    while True:
        result = status.check_ci(pr, repo=repo)
        verdict = result.verdict

        if verdict is CIStatus.PENDING:
            consecutive_errors = 0
            limbo_reported = _report_limbo(result, limbo_reported)
            if _deadline_hit(deadline, pr, timeout, what=_pending_reason(result)):
                _limbo_timeout_note(pr, result)
                return 1
            time.sleep(every)
            continue

        if verdict is CIStatus.NO_CHECKS:
            # Just-pushed window: the rollup can be empty for a few seconds after a push before
            # Actions attaches its first check. Wait quietly; --timeout bounds the wait.
            # (NO_WORKFLOW_RUNS, by contrast, means Actions confirmed it never scheduled — that is
            # a verdict below, not a wait.)
            if not no_checks_reported:
                print("[ci] no checks yet — waiting", file=sys.stderr, flush=True)
                no_checks_reported = True
            consecutive_errors = 0
            if _deadline_hit(deadline, pr, timeout):
                return 1
            time.sleep(every)
            continue

        if verdict is CIStatus.ERROR:
            consecutive_errors += 1
            print(
                f"[ci] poll error ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): "
                f"{result.error_detail}",
                file=sys.stderr,
                flush=True,
            )
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(
                    f"PR #{pr} CI error after {MAX_CONSECUTIVE_ERRORS} attempts: "
                    f"{result.error_detail}",
                    file=sys.stderr,
                    flush=True,
                )
                return 3
            if _deadline_hit(deadline, pr, timeout):
                return 1
            time.sleep(every)
            continue

        # Settled verdict — FAILED / NOT_READY / MERGE_CONFLICT / NO_WORKFLOW_RUNS / ALL_PASSED
        return _conclude_settled(result, pr)
