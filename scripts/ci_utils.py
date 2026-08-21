#!/usr/bin/env python3
"""CI check utilities — reusable, correct logic for GitHub CI status polling.

Use this from watcher scripts or agent code instead of writing ad-hoc
conclusion checks.  The core function `check_ci()` returns a structured
result that cannot be misinterpreted.

Detects merge conflicts — when a PR has conflicts, CI runs are blocked
and the script reports MERGE_CONFLICT instead of hanging on PENDING.

Distinguishes "Actions never scheduled" (NO_WORKFLOW_RUNS — not green, stop and
investigate) from "Actions is scheduled but has not attached a check yet"
(PENDING — keep waiting). The rollup alone cannot tell them apart: for the first
seconds after a push both look like a rollup carrying no workflow checks, and
that window is exactly when a poll right after pushing lands.

Usage as library:
    from ci_utils import check_ci, CIStatus

    status = check_ci("502")  # repo defaults to the checkout's own remote
    if status.verdict == CIStatus.MERGE_CONFLICT:
        print(f"PR has merge conflicts — rebase first!")
    elif status.verdict == CIStatus.FAILED:
        print(f"CI failed: {status.failed}")
    elif status.verdict == CIStatus.ALL_PASSED:
        print("All checks passed!")
    elif status.verdict == CIStatus.PENDING:
        print(f"Still waiting: {status.pending}")

Usage as CLI:
    .venv/bin/python scripts/ci_utils.py <PR_NUMBER> [--repo owner/repo] [--json]
    .venv/bin/python scripts/ci_utils.py <PR_NUMBER> --wait [--timeout N] [--merge]

    Default: query once and exit (PENDING prints and exits 0 — a one-shot
    probe, not a poller). `--wait`: poll until the verdict settles, then exit
    with the monitor contract — 0 green, 1 not green (or timed out), 3
    persistent gh/network errors, 4 enqueue failed. `--merge` implies `--wait`
    and enqueues the PR once green (Mergify merge queue — posts `@mergifyio
    queue` on the PR, or `@mergifyio requeue` if the Mergify check shows the PR
    was previously queued and dequeued, e.g. by a force-push; Mergify rebases
    onto latest main, re-runs CI on the rebased head, and rebase-merges it
    once .mergify.yml merge_conditions are green), then waits for the queue to
    finish merging. The all-green predicate excludes checks named "Mergify
    Merge Queue" — that check reports queue state, not a CI result, and sits
    pending forever after a dequeue, which used to deadlock this wait (issue
    #98). This is the canonical CI watcher: launch it with
    ava.shell.run_background, and the completion notice delivers the exit
    code + verdict to the agent automatically.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

# Allow `python scripts/ci_utils.py` (sys.path[0] = scripts/) to find the
# sibling module; under pytest pythonpath=["."] this is a redundant no-op.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.ci_job_rerun import list_failed_jobs, rerun_failed_jobs


class CIStatus(Enum):
    ALL_PASSED = "all_passed"  # every COMPLETED check is SUCCESS / SKIPPED / NEUTRAL
    FAILED = "failed"  # at least one COMPLETED check has a failing conclusion
    PENDING = "pending"  # some checks are still QUEUED / IN_PROGRESS / PENDING
    MERGE_CONFLICT = "merge_conflict"  # PR has merge conflicts — CI blocked
    NO_CHECKS = "no_checks"  # statusCheckRollup is empty
    NO_WORKFLOW_RUNS = "no_workflow_runs"  # checks exist, but Actions produced none
    ERROR = "error"  # gh CLI / network / JSON error

    @property
    def is_terminal(self) -> bool:
        """True for a settled verdict a watcher must act on.

        PENDING is the only transitional state; every other verdict — green,
        failed, conflicted, did-not-run, errored — is final and should end a
        watch loop. Watchers compare `verdict.is_terminal` instead of
        hard-coding string values, so a mistyped verdict can never silently
        spin forever (the 2026-08-03 incident: a watcher waiting on
        "success"/"failure" strings that do not exist)."""
        return self is not CIStatus.PENDING


# ---- Conclusion classification ----

PASSING = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
FAILING = frozenset(
    {
        "FAILURE",
        "CANCELLED",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
        "TIMED_OUT",
    }
)

# mergeable values that indicate a conflict
CONFLICTING = frozenset({"CONFLICTING"})

# ---- --wait (poller) defaults ----
# The canonical repo: derived from the checkout's origin remote when run
# inside a git checkout (forks get their own owner/repo for free), falling
# back to a hard-coded default for contexts without a git remote (watcher
# processes run from the agent workspace, which is not a checkout).
# Overridable with --repo.
_FALLBACK_REPO = "zhiyuan-zhang0206/Ava"


def _derive_repo() -> str:
    """owner/repo from the checkout's origin remote, else the fallback."""
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        m = re.search(r"(?:github\.com[:/])([^/]+)/([^/]+?)(?:\.git)?$", out)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    except Exception:  # noqa: S110 - best-effort lookup; fall back
        pass
    return _FALLBACK_REPO


DEFAULT_REPO = _derive_repo()
POLL_INTERVAL = 30  # seconds between polls
MAX_CONSECUTIVE_ERRORS = 3


@dataclass
class CIResult:
    verdict: CIStatus
    checks: list[dict] = field(default_factory=list)

    # Derived convenience fields (populated by check_ci)
    completed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)  # {name, conclusion}
    workflow_checks: list[str] = field(default_factory=list)  # checks a workflow run produced
    # Checks whose name starts with "Mergify" (issue #98): queue state, not a
    # CI result — excluded from completed/pending/passed/failed so a check
    # that sits pending forever after a dequeue can never block the all-green
    # wait. Kept here so `--merge` can inspect queue state separately.
    mergify_checks: list[dict] = field(default_factory=list)
    mergeable: str = ""  # MERGEABLE / CONFLICTING / UNKNOWN
    head_sha: str = ""
    error_detail: str = ""

    def summary(self) -> str:
        """One-line human-readable summary."""
        if self.verdict == CIStatus.MERGE_CONFLICT:
            return "PR has merge conflicts — rebase onto latest main first"
        if self.verdict == CIStatus.ALL_PASSED:
            return f"CI all green ({len(self.passed)} checks passed)"
        if self.verdict == CIStatus.FAILED:
            names = [c["name"] for c in self.failed]
            return f"CI FAILED: {', '.join(names)}"
        if self.verdict == CIStatus.PENDING:
            if len(self.pending) > 3:
                return f"CI pending: {len(self.pending)} still running ({', '.join(self.pending[:3])}...)"
            return f"CI pending: {', '.join(self.pending)}"
        if self.verdict == CIStatus.NO_CHECKS:
            return "No checks found"
        if self.verdict == CIStatus.NO_WORKFLOW_RUNS:
            names = ", ".join(self.passed) or "none"
            return (
                f"CI DID NOT RUN: no workflow produced a check ({len(self.passed)} "
                f"non-workflow check(s) reporting: {names}). Not green — investigate "
                "why Actions did not schedule before merging."
            )
        return f"Error: {self.error_detail}"


def _repo_has_workflows() -> bool:
    """True when this checkout defines GitHub Actions workflows.

    Bounds the NO_WORKFLOW_RUNS guard to repos where workflow checks are
    actually expected — a repo with no workflows at all is legitimately green on
    app checks alone.
    """
    wf_dir = Path(__file__).resolve().parent.parent / ".github" / "workflows"
    return any(wf_dir.glob("*.yml")) or any(wf_dir.glob("*.yaml"))


MERGIFY_CHECK_PREFIX = "Mergify"


def _partition_checks(checks: list[dict], result: CIResult) -> None:
    """Sort each rollup check into completed / passed / failed / pending on
    `result`, and record which of them a workflow produced.

    Only COMPLETED checks are judged: a QUEUED / IN_PROGRESS one is pending, and
    so is a COMPLETED one whose conclusion is unrecognized — guessing there is
    how a false "all green" gets reported.

    A check named "Mergify Merge Queue" reports queue state, not a CI result
    (issue #98) — it sits pending indefinitely once a PR is dequeued (e.g. a
    force-push knocks it out of the queue), which would otherwise deadlock
    `--wait --merge` forever waiting for a check that only queuing itself can
    turn green. Routed to `result.mergify_checks` instead, so it never enters
    completed/pending/passed/failed/workflow_checks.
    """
    for c in checks:
        name = c.get("name", "?")
        status = c.get("status", "")
        conclusion = c.get("conclusion", "")

        if name.startswith(MERGIFY_CHECK_PREFIX):
            result.mergify_checks.append(c)
            continue

        # A check produced by a workflow run carries the workflow's name; checks
        # posted by a GitHub App (GitGuardian, coverage bots) leave it empty.
        # This is what tells "the suite ran and passed" apart from "the suite
        # never started and an app happened to report".
        if c.get("workflowName"):
            result.workflow_checks.append(name)

        if status != "COMPLETED":
            result.pending.append(name)
            continue

        result.completed.append(name)
        if conclusion in FAILING:
            result.failed.append({"name": name, "conclusion": conclusion})
        elif conclusion in PASSING:
            result.passed.append(name)
        else:
            result.pending.append(name)


def _runs_not_yet_reporting(head_sha: str, repo: str | None) -> list[str]:
    """Names of workflow runs for `head_sha` that are scheduled but have not
    attached a check to the commit yet.

    `statusCheckRollup` cannot tell "Actions will never produce a check" from
    "Actions has not produced one *yet*": between a push and the first check-run
    appearing, both look like a rollup with no workflow checks in it. That gap is
    seconds wide and lands exactly on the first poll after a push, which is when
    an agent is most likely to be watching — and NO_WORKFLOW_RUNS reads as "stop,
    investigate".

    The runs API answers what the rollup cannot: a run in `queued` /
    `in_progress` / `requested` / `waiting` for this sha means checks are coming.
    An empty list means nothing is scheduled, which is the real failure the
    NO_WORKFLOW_RUNS guard exists for.

    On any error this returns empty — the caller then keeps its conservative
    not-green verdict rather than inventing a reason to wait.
    """
    r = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runs?head_sha={head_sha}"
            if repo
            else f"repos/{{owner}}/{{repo}}/actions/runs?head_sha={head_sha}",
            "--jq",
            '[.workflow_runs[] | select(.status != "completed") | .name] | @json',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return []
    try:
        names = json.loads(r.stdout.strip() or "[]")
    except json.JSONDecodeError:
        return []
    return [str(n) for n in names] if isinstance(names, list) else []


# ---- Dequeue detection (issue #98) ----
# `gh pr view --json statusCheckRollup` only exposes name/status/conclusion for
# a check — not the human-readable text Mergify writes explaining queue state
# (e.g. "removed from the queue"). The REST check-runs API carries that text as
# `output.title`, so a dequeue is read from there instead of guessed from the
# rollup.

_MERGIFY_CHECK_NAME = "Mergify Merge Queue"
_DEQUEUE_TITLE_MARKERS = ("removed from the queue", "dequeued", "unqueued")


def _fetch_mergify_check_title(head_sha: str, repo: str | None) -> str | None:
    """Best-effort read of the Mergify Merge Queue check-run's `output.title`
    for `head_sha`. Returns None on any error, empty result, or missing
    head_sha — the caller then defaults to `queue`, the safe choice for
    "state unknown"."""
    if not head_sha:
        return None
    r = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            f"repos/{repo}/commits/{head_sha}/check-runs"
            if repo
            else f"repos/{{owner}}/{{repo}}/commits/{head_sha}/check-runs",
            "--jq",
            f'[.check_runs[] | select(.name=="{_MERGIFY_CHECK_NAME}")][0].output.title',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    title = r.stdout.strip()
    return title if title and title != "null" else None


def _mergify_was_dequeued(title: str | None) -> bool:
    """True when the Mergify check's output title reads as a dequeue (a PR
    that was queued and got knocked out — e.g. by a force-push) rather than
    "never queued" or "currently queued / merged"."""
    if not title:
        return False
    lowered = title.lower()
    return any(marker in lowered for marker in _DEQUEUE_TITLE_MARKERS)


def _queue_command(repo: str, head_sha: str) -> str:
    """`@mergifyio queue` for a PR entering the queue fresh, `@mergifyio
    requeue` for one that was already queued and got dequeued (issue #98: a
    force-push after queuing auto-dequeues the PR, and `queue` cannot recover
    it — `requeue` is the verb Mergify defines for that case)."""
    title = _fetch_mergify_check_title(head_sha, repo)
    return "@mergifyio requeue" if _mergify_was_dequeued(title) else "@mergifyio queue"


def check_ci(pr_number: str | int, *, repo: str | None = None) -> CIResult:
    """Poll one PR's CI status and mergeability via `gh pr view --json`.

    Returns a CIResult with a clear verdict — no ambiguous exit codes
    that agents misinterpret.

    Merge conflict detection: queries ``mergeable`` alongside
    ``statusCheckRollup``.  When mergeable == "CONFLICTING" the verdict
    is MERGE_CONFLICT — CI runs are blocked until the conflict is
    resolved, so there is no point waiting.

    The key rule: only COMPLETED checks are evaluated.  Checks that are
    QUEUED / IN_PROGRESS / PENDING are correctly identified as such and
    do NOT trigger a FAILED verdict.
    """
    pr_num = str(pr_number)
    result = CIResult(verdict=CIStatus.ERROR)

    # No --repo => gh resolves the PR against the current checkout's remote, so
    # this works in any clone or fork without a hard-coded slug.
    r = subprocess.run(  # noqa: S603
        [
            "gh",
            "pr",
            "view",
            pr_num,
            *(("--repo", repo) if repo else ()),
            "--json",
            "mergeable,statusCheckRollup,headRefOid",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        result.error_detail = f"gh CLI error: {r.stderr.strip()}"
        return result

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        result.error_detail = f"JSON parse error: {e}"
        return result

    result.head_sha = data.get("headRefOid", "")

    # --- Merge conflict detection ---
    result.mergeable = data.get("mergeable", "UNKNOWN")
    if result.mergeable in CONFLICTING:
        result.verdict = CIStatus.MERGE_CONFLICT
        # Still populate checks for visibility
        checks = data.get("statusCheckRollup", [])
        result.checks = checks
        for c in checks:
            name = c.get("name", "?")
            status = c.get("status", "")
            if status == "COMPLETED":
                result.completed.append(name)
            else:
                result.pending.append(name)
        return result

    # --- CI check evaluation ---
    checks = data.get("statusCheckRollup", [])
    result.checks = checks

    if not checks:
        result.verdict = CIStatus.NO_CHECKS
        return result

    _partition_checks(checks, result)

    # Determine verdict
    if result.failed:
        result.verdict = CIStatus.FAILED
    elif result.pending:
        result.verdict = CIStatus.PENDING
    elif not result.workflow_checks and _repo_has_workflows():
        # Everything present passed — but nothing present came from a workflow,
        # while this checkout does define workflows. Either the suite did not run
        # (reporting ALL_PASSED here is how a broken `runs-on` — 2026-07-28,
        # hosted runners a private repo could not schedule — reads as green: the
        # only check left standing was a GitHub App's, and it passed), or it is
        # scheduled and has not attached a check yet. Only the runs API can tell
        # those apart.
        scheduled = _runs_not_yet_reporting(data.get("headRefOid", ""), repo)
        if scheduled:
            result.pending.extend(scheduled)
            result.verdict = CIStatus.PENDING
        else:
            result.verdict = CIStatus.NO_WORKFLOW_RUNS
    else:
        # All completed, none failed
        result.verdict = CIStatus.ALL_PASSED

    return result


# ---- CLI ----


def _query_once(pr: str, repo: str, *, as_json: bool) -> int:
    """One-shot probe: print the current verdict and exit (legacy contract —
    PENDING / NO_CHECKS / ERROR exit 0, FAILED / MERGE_CONFLICT /
    NO_WORKFLOW_RUNS exit 1)."""
    result = check_ci(pr, repo=repo)

    if as_json:
        print(
            json.dumps(
                {
                    "verdict": result.verdict.value,
                    "mergeable": result.mergeable,
                    "completed": result.completed,
                    "pending": result.pending,
                    "passed": result.passed,
                    "failed": result.failed,
                    "workflow_checks": result.workflow_checks,
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
        if result.verdict in (CIStatus.FAILED, CIStatus.MERGE_CONFLICT, CIStatus.NO_WORKFLOW_RUNS)
        else 0
    )


def _deadline_hit(deadline: float | None, pr: str, timeout: int) -> bool:
    """True once `deadline` passes — the --timeout bound for --wait use (which
    has no watchdog). Prints the reason so the completion notice carries it."""
    if deadline is None:
        return False
    if time.monotonic() >= deadline:
        print(
            f"PR #{pr} CI still pending after {timeout}s — timed out.",
            file=sys.stderr,
            flush=True,
        )
        return True
    return False


def _enqueue_pr(pr: str, repo: str, command: str = "@mergifyio queue") -> int:
    """--merge: enqueue the PR once CI is green.

    Mergify drives this repo's merge queue (GitHub's own queue does not
    support personal private repos). Enqueuing = posting `command` (`@mergifyio
    queue` for a PR entering fresh, `@mergifyio requeue` for one that was
    dequeued — see `_queue_command`, issue #98) as a PR comment; Mergify then
    rebases the PR onto latest main, re-runs CI on the rebased head via the
    ordinary pull_request event, and lands it with a rebase merge (linear
    history) once its merge_conditions (see .mergify.yml) are green —
    replacing the old manual rebase-and-repoll loop. The remote branch is NOT
    deleted automatically; clean up with `git push origin --delete <branch>`.

    The gh call carries --repo explicitly (same reason as check_ci — no cwd
    dependency)."""
    r = subprocess.run(  # noqa: S603
        ["gh", "pr", "comment", pr, "--repo", repo, "--body", command],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        print(f"PR #{pr} enqueue failed: {r.stderr.strip()}", file=sys.stderr, flush=True)
        return 4
    print(f"PR #{pr} enqueued with Mergify ({command}): {r.stdout.strip()}")
    return 0


def _wait_for_merge(pr: str, repo: str, every: int, timeout: int) -> int:
    """After enqueue: poll until the merge queue lands the PR.

    state=MERGED is the success signal. CLOSED without merging means something
    cancelled it. Anything else is the queue still working (rebase + CI +
    merge can take 10-30 min in a busy queue) — bounded by `timeout` like the
    rest of --wait, so a wedged queue is reported instead of polled forever.
    """
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        r = subprocess.run(  # noqa: S603
            ["gh", "pr", "view", pr, "--repo", repo, "--json", "state"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            print(
                f"[ci] merge-watch poll error: {r.stderr.strip()}",
                file=sys.stderr,
                flush=True,
            )
            return 3
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            print(
                f"[ci] merge-watch poll JSON error: {r.stdout[:200]}",
                file=sys.stderr,
                flush=True,
            )
            return 3
        state = data.get("state")
        if state == "MERGED":
            print(f"PR #{pr} merged by the merge queue")
            return 0
        if state == "CLOSED":
            print(f"PR #{pr} CLOSED without merging — investigate", file=sys.stderr, flush=True)
            return 1
        if _deadline_hit(deadline, pr, timeout):
            print(
                f"PR #{pr} still in the queue after {timeout}s — check it manually",
                file=sys.stderr,
                flush=True,
            )
            return 1
        time.sleep(every)


def _wait_for_verdict(pr: str, repo: str, every: int, timeout: int, *, merge: bool) -> int:
    """Poll check_ci until the verdict settles, then report and exit.

    Never loops silently: a persistent gh/network failure exits 3 after
    MAX_CONSECUTIVE_ERRORS attempts with the error printed — the silent
    infinite loop this was built to eliminate (2026-08-02, PR #1243).
    """
    consecutive_errors = 0
    no_checks_reported = False
    deadline = time.monotonic() + timeout if timeout else None

    while True:
        result = check_ci(pr, repo=repo)
        verdict = result.verdict

        if verdict is CIStatus.PENDING:
            consecutive_errors = 0
            if _deadline_hit(deadline, pr, timeout):
                return 1
            time.sleep(every)
            continue

        if verdict is CIStatus.NO_CHECKS:
            # Just-pushed window: the rollup can be empty for a few seconds
            # after a push before Actions attaches its first check. Wait
            # quietly; --timeout bounds the wait. (NO_WORKFLOW_RUNS, by
            # contrast, means Actions confirmed it never scheduled — that is
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

        # Settled verdict — FAILED / MERGE_CONFLICT / NO_WORKFLOW_RUNS / ALL_PASSED
        if verdict is CIStatus.ALL_PASSED:
            print(f"PR #{pr} CI green: {result.summary()}")
            if merge:
                command = _queue_command(repo, result.head_sha)
                rc = _enqueue_pr(pr, repo, command)
                if rc != 0:
                    return rc
                return _wait_for_merge(pr, repo, every, timeout)
            return 0

        print(f"PR #{pr} CI NOT green: {result.summary()}", file=sys.stderr, flush=True)
        if result.failed:
            names = ", ".join(c.get("name", "?") for c in result.failed)
            print(f"PR #{pr} failed checks: {names}", file=sys.stderr, flush=True)
        if result.error_detail:
            print(f"PR #{pr} detail: {result.error_detail}", file=sys.stderr, flush=True)
        return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry. Default: one-shot query (legacy behavior). `--wait`: poll
    until the verdict settles with the monitor exit-code contract; `--merge`
    implies `--wait` and squash-merges once green."""
    import argparse

    p = argparse.ArgumentParser(description="Check CI status of a GitHub PR")
    p.add_argument("pr", help="PR number")
    p.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"owner/repo (default: {DEFAULT_REPO})",
    )
    p.add_argument("--json", action="store_true", help="Output full JSON (one-shot only)")
    p.add_argument(
        "--wait",
        action="store_true",
        help="poll until the verdict settles instead of querying once",
    )
    p.add_argument(
        "--every",
        type=int,
        default=POLL_INTERVAL,
        help=f"with --wait: poll interval in seconds (default: {POLL_INTERVAL})",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="with --wait: stop after N seconds still pending (0 = forever)",
    )
    p.add_argument(
        "--merge",
        action="store_true",
        help="with --wait: enqueue the PR once CI is green and wait for the "
        "merge queue to land it (implies --wait; default timeout 1800s)",
    )
    p.add_argument(
        "--rerun-failed-jobs",
        action="store_true",
        help="re-run every failed job of the PR's workflow runs at JOB level "
        "(issue #102): run-level rerun is refused while any job is still "
        "going, so recovery must not wait on the slowest surviving shard. "
        "Exit 0 when all failed jobs were re-run (or none were), 3 on errors. "
        "Exclusive with --wait/--merge/--json.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="with --rerun-failed-jobs: list failed jobs without re-running them",
    )
    args = p.parse_args(argv)
    if args.every <= 0:
        p.error("--every must be positive")
    if args.timeout < 0:
        p.error("--timeout must be >= 0")
    if args.json and args.wait:
        p.error("--json and --wait are mutually exclusive")

    if args.merge:
        args.wait = True
        # Enqueue-to-landed can take 10-30 min in a busy queue; bound the wait
        # unless the caller explicitly passed --timeout.
        if args.timeout == 0:
            args.timeout = 1800

    if args.rerun_failed_jobs:
        if args.wait or args.merge or args.json:
            p.error("--rerun-failed-jobs is exclusive with --wait/--merge/--json")
        if args.dry_run:
            jobs = list_failed_jobs(args.pr, args.repo)
            if not jobs:
                print("No failed jobs to re-run")
                return 0
            for j in jobs:
                print(f"{j['name']} (job {j['job_id']}, run {j['run_id']}, {j['conclusion']})")
            return 0
        reran, errors = rerun_failed_jobs(args.pr, args.repo)
        for j in reran:
            print(f"Re-ran {j['name']} (job {j['job_id']})")
        for e in errors:
            print(f"Re-run failed: {e}")
        return 3 if errors else 0

    if args.wait:
        return _wait_for_verdict(args.pr, args.repo, args.every, args.timeout, merge=args.merge)
    return _query_once(args.pr, args.repo, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
