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

Usage as CLI:
    .venv/bin/python scripts/ci_utils.py <PR_NUMBER> [--repo owner/repo] [--json]
    .venv/bin/python scripts/ci_utils.py <PR_NUMBER> --wait [--timeout N] [--merge]

    Default: query once and exit (PENDING prints and exits 0 — a one-shot
    probe, not a poller). `--wait`: poll until the verdict settles, then exit
    with the monitor contract — 0 green, 1 not green (or timed out), 3
    persistent gh/network errors, 4 enqueue failed or was rejected twice.
    `--merge` implies `--wait`
    and enqueues the PR once green. `--queue` (or `CI_QUEUE`) selects Mergify
    (the gray-phase default) or Trunk; `--priority` maps the Trunk submission
    priority, which requires `TRUNK_API_TOKEN`. Mergify posts `@mergifyio
    queue` (or `@mergifyio requeue` after a dequeue); Trunk submits the PR then
    polls its queue state. Both wait at least five minutes after a head update
    before queueing. The all-green predicate excludes queue-state checks named
    "Mergify Merge Queue" and "Trunk Merge Queue", plus "qa-approved-gate"
    (a QA gate the queue enforces at merge time). This is the canonical CI
    watcher: launch it with ava.shell.run_background, and the completion notice
    delivers the exit code + verdict to the agent automatically.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import ceil
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
        """True for every settled verdict; PENDING alone is transitional."""
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
# Derived from origin when possible; overridable with --repo.
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
QUEUE_COOLDOWN_SECONDS = 300
RETRY_BACKOFF_SECONDS = 300
_MERGIFY_BOT_LOGINS = frozenset({"mergify", "mergify[bot]"})
_QUEUE_CHOICES = ("trunk", "mergify")
_QUEUE_NAMES = frozenset(_QUEUE_CHOICES)
_TRUNK_PRIORITIES = {"urgent": 0, "high": 10, "medium": 100, "low": 200}
_TRUNK_API_BASE_URL = "https://api.trunk.io/v1"
_TRUNK_REQUEST_TIMEOUT_SECONDS = 30


def _resolve_queue(queue: str | None) -> str:
    """Return the CLI-selected queue, then CI_QUEUE, then the gray default."""
    resolved = queue or os.environ.get("CI_QUEUE") or "mergify"
    if resolved not in _QUEUE_NAMES:
        raise ValueError(f"unknown CI queue: {resolved}")
    return resolved


def _trunk_priority(priority: str) -> int:
    """Map the user-facing Trunk priority to its REST API integer."""
    return _TRUNK_PRIORITIES[priority]


def _parse_ts(iso: str | None) -> float | None:
    """Parse a GitHub RFC3339 timestamp, returning None for bad probes."""
    try:
        return datetime.fromisoformat(iso.strip().strip('"')).timestamp() if iso else None
    except (AttributeError, TypeError, ValueError):
        return None


def _last_head_update(pr: str, repo: str) -> float | None:
    """Best-effort newest force-push event or head-commit committer date."""
    probes = (
        (
            f"repos/{repo}/issues/{pr}/timeline",
            "--paginate",
            '[.[] | select(.event == "head_ref_force_pushed")] | .[-1].created_at',
        ),
        (f"repos/{repo}/pulls/{pr}/commits", "", ".[-1].commit.committer.date"),
    )
    timestamps: list[float] = []
    for endpoint, paginate, jq in probes:
        r = subprocess.run(  # noqa: S603
            ["gh", "api", endpoint, *((paginate,) if paginate else ()), "--jq", jq],
            capture_output=True,
            text=True,
            check=False,
        )
        parsed = _parse_ts(r.stdout) if r.returncode == 0 else None
        if parsed is not None:
            timestamps.append(parsed)
    return max(timestamps, default=None)


def _queue_cooldown_seconds(pr: str, repo: str) -> int:
    last = _last_head_update(pr, repo)
    return 0 if last is None else max(0, ceil(QUEUE_COOLDOWN_SECONDS - (time.time() - last)))


def _bot_rejection_reason(data: dict, since_ts: float) -> str | None:
    """Return the newest rejection unless an in-window confirmation proves success.

    A "the merge queue status continues" confirmation wins: the PR is in the
    queue, so a later rejection can be a force-push dequeue or another agent's
    command hitting the lock. A standalone dequeue notification ("has been
    dequeued", e.g. "merge conditions no longer match") counts as a rejection
    too — it carries no quoted queue command. Retrying feeds the storm; state
    watch lands or times out."""
    comments = data.get("comments", []) if isinstance(data, dict) else []
    if not isinstance(comments, list):
        return None
    markers = (
        "removed from the queue",
        "left the queue",
        "manually updated",
        "already running from a previous command",
        "cannot be queued",
        "merge conditions no longer match",
    )
    rejection = None
    for comment in reversed(comments):
        if not isinstance(comment, dict):
            continue
        author = comment.get("author") or {}
        created = _parse_ts(comment.get("createdAt"))
        if (
            not isinstance(author, dict)
            or author.get("login") not in _MERGIFY_BOT_LOGINS
            or created is None
            or created < since_ts
        ):
            continue
        body = str(comment.get("body") or "").lower()
        if "the merge queue status continues" in body:
            return None
        if body.startswith("<!---\ndo not edit\n-*- mergify payload -*-"):
            continue
        if rejection is None and "queue-control:queue" in body:
            rejection = "checkbox (command not executed)"
        elif rejection is None and ("> queue" in body or "> requeue" in body):
            rejection = next((m for m in markers if m in body), None)
        elif rejection is None and "has been dequeued" in body:
            # Mergify's standalone dequeue notification (no quoted command):
            # "Pull request #<n> has been dequeued — merge conditions no longer
            # match. Blocked by: ...". The payload status comment is skipped
            # above, so this branch only sees the notification form.
            rejection = "dequeued (merge conditions no longer match)"
    return rejection


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
    # Trunk's queue-state check is likewise not a CI result and must not turn
    # an otherwise green PR into a perpetual PENDING verdict.
    trunk_checks: list[dict] = field(default_factory=list)
    # The qa-approved label gate is enforced by the queue at merge time, not by
    # the CI verdict. Kept separately so it cannot enter the verdict buckets.
    gate_checks: list[dict] = field(default_factory=list)
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
TRUNK_MERGE_QUEUE_CHECK_NAME = "Trunk Merge Queue"
# Must match the job name in .github/workflows/qa-approved-gate.yml — the check is
# a QA label gate enforced by the queue at merge time, never part of the CI verdict.
QA_APPROVED_GATE_CHECK_NAME = "qa-approved-gate"


def _partition_checks(checks: list[dict], result: CIResult) -> None:
    """Sort each rollup check into completed / passed / failed / pending on
    `result`, and record which of them a workflow produced.

    Only COMPLETED checks are judged: a QUEUED / IN_PROGRESS one is pending, and
    so is a COMPLETED one whose conclusion is unrecognized — guessing there is
    how a false "all green" gets reported.

    Mergify and Trunk checks report queue state rather than CI results, while
    `qa-approved-gate` is a QA gate enforced by the queue at merge time. All
    are routed to dedicated buckets so they never enter the verdict fields.
    """
    for c in checks:
        name = c.get("name", "?")
        status = c.get("status", "")
        conclusion = c.get("conclusion", "")

        if name.startswith(MERGIFY_CHECK_PREFIX):
            result.mergify_checks.append(c)
            continue
        if name == TRUNK_MERGE_QUEUE_CHECK_NAME:
            result.trunk_checks.append(c)
            continue
        if name == QA_APPROVED_GATE_CHECK_NAME:
            result.gate_checks.append(c)
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
            if name == QA_APPROVED_GATE_CHECK_NAME:
                result.gate_checks.append(c)
                continue
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
                    "mergify_checks": result.mergify_checks,
                    "gate_checks": result.gate_checks,
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


def _enqueue_pr(pr: str, repo: str, command: str = "@mergifyio queue") -> int:
    """--merge: enqueue the PR once CI is green.

    Enqueuing posts `command` as a PR comment. The gh call carries --repo
    explicitly so it never depends on the current directory."""
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


def _trunk_pr_payload(pr: str, repo: str) -> dict[str, object]:
    """Build the repository and PR identity required by Trunk's API."""
    owner, name = repo.split("/", maxsplit=1)
    return {
        "repo": {"host": "github.com", "owner": owner, "name": name},
        "pr": {"number": int(pr)},
        "targetBranch": "main",
    }


def _trunk_post(
    endpoint: str, payload: dict[str, object], token: str
) -> tuple[dict[str, object] | None, str | None]:
    """POST one Trunk API request, returning its object response or an error."""
    request = urllib.request.Request(  # noqa: S310 - fixed HTTPS Trunk API endpoint
        f"{_TRUNK_API_BASE_URL}/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "x-api-token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - request uses the fixed HTTPS endpoint above
            request, timeout=_TRUNK_REQUEST_TIMEOUT_SECONDS
        ) as response:
            status = response.status
            body = response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        return None, str(error)
    if status != 200:
        return None, f"HTTP {status}"
    if not body:
        return {}, None
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        # submitPullRequest / cancelPullRequest answer 200 with a plain-text
        # "OK" body (verified live 2026-09-01); a 200 is a success regardless
        # of body shape, so a non-JSON body must not be read as an error.
        return {}, None
    if not isinstance(data, dict):
        return None, "response was not a JSON object"
    return data, None


def _submit_trunk(pr: str, repo: str, priority: str, *, token: str) -> int:
    """Submit a green PR to Trunk, retrying one failed submission."""
    payload = _trunk_pr_payload(pr, repo)
    payload.update({"priority": _trunk_priority(priority), "noBatch": False})
    for attempt in range(2):
        _, error = _trunk_post("submitPullRequest", payload, token)
        if error is None:
            print(f"PR #{pr} submitted to the Trunk merge queue", file=sys.stderr, flush=True)
            return 0
        print(
            f"[ci] Trunk queue submit error ({attempt + 1}/2): {error}",
            file=sys.stderr,
            flush=True,
        )
        if attempt == 1:
            print(f"PR #{pr} Trunk queue submission failed", file=sys.stderr, flush=True)
            return 4
        print(
            f"retrying Trunk queue submission in {RETRY_BACKOFF_SECONDS}s",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(RETRY_BACKOFF_SECONDS)
    return 4


def _watch_trunk_enqueue(
    pr: str,
    repo: str,
    *,
    every: int,
    deadline: float | None,
    timeout: int,
    token: str,
) -> int:
    """Poll Trunk's submitted-PR state until it merges, fails, or times out."""
    payload = _trunk_pr_payload(pr, repo)
    consecutive_errors = 0
    while True:
        data, error = _trunk_post("getSubmittedPullRequest", payload, token)
        if error is not None:
            consecutive_errors += 1
            print(
                f"[ci] Trunk queue poll error ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {error}",
                file=sys.stderr,
                flush=True,
            )
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(
                    f"PR #{pr} Trunk queue error after {MAX_CONSECUTIVE_ERRORS} attempts: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                return 3
        else:
            if data is None:
                print("[ci] Trunk queue poll returned no data", file=sys.stderr, flush=True)
                return 3
            consecutive_errors = 0
            state = data.get("state")
            if state == "merged":
                print(f"PR #{pr} merged by the Trunk merge queue")
                return 0
            if state in {"failed", "cancelled"}:
                reason = data.get("reason") or state
                print(f"PR #{pr} Trunk queue {state}: {reason}", file=sys.stderr, flush=True)
                return 1
            print(f"[ci] Trunk queue state: {state or 'unknown'}", file=sys.stderr, flush=True)
        if _deadline_hit(deadline, pr, timeout, "still in the Trunk merge queue"):
            return 1
        time.sleep(every)


def _trunk_merge_flow(
    pr: str, repo: str, priority: str, *, every: int, timeout: int, token: str
) -> int:
    """Apply the standard cooldown/green recheck, then submit and watch Trunk."""
    deadline = time.monotonic() + timeout if timeout else None
    while (remaining := _queue_cooldown_seconds(pr, repo)) > 0:
        if _deadline_hit(deadline, pr, timeout, "queue cooldown not finished"):
            return 1
        print(
            f"PR #{pr} head updated {remaining}s ago — queue cooldown, waiting",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(min(remaining, every))

    result = check_ci(pr, repo=repo)
    if result.verdict is CIStatus.ERROR:
        print(
            f"PR #{pr} CI error before queueing: {result.error_detail}",
            file=sys.stderr,
            flush=True,
        )
        return 3
    if result.verdict is not CIStatus.ALL_PASSED:
        print(f"PR #{pr} CI no longer green: {result.summary()}", file=sys.stderr, flush=True)
        return 1

    rc = _submit_trunk(pr, repo, priority, token=token)
    if rc != 0:
        return rc
    return _watch_trunk_enqueue(
        pr, repo, every=every, deadline=deadline, timeout=timeout, token=token
    )


def _watch_enqueue(
    pr: str, repo: str, command_time: float, every: int, deadline: float | None, timeout: int
) -> tuple[int, bool]:
    """Watch PR state and post-command Mergify replies in one poll."""
    while True:
        r = subprocess.run(  # noqa: S603
            ["gh", "pr", "view", pr, "--repo", repo, "--json", "state,comments"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            print(f"[ci] merge-watch poll error: {r.stderr.strip()}", file=sys.stderr, flush=True)
            return 3, False
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            print(
                f"[ci] merge-watch poll JSON error: {r.stdout[:200]}", file=sys.stderr, flush=True
            )
            return 3, False
        if not isinstance(data, dict):
            print(
                f"[ci] merge-watch poll JSON error: {r.stdout[:200]}", file=sys.stderr, flush=True
            )
            return 3, False
        state = data.get("state")
        if state == "MERGED":
            print(f"PR #{pr} merged by the merge queue")
            return 0, False
        if state == "CLOSED":
            print(f"PR #{pr} CLOSED without merging — investigate", file=sys.stderr, flush=True)
            return 1, False
        if reason := _bot_rejection_reason(data, command_time):
            print(
                f"PR #{pr} queue command rejected by Mergify: {reason}", file=sys.stderr, flush=True
            )
            return 0, True
        if _deadline_hit(deadline, pr, timeout, "still in the merge queue"):
            return 1, False
        time.sleep(every)


def _merge_flow(pr: str, repo: str, every: int, timeout: int) -> int:
    """Cooldown, re-check CI, enqueue, and retry one rejected command."""
    deadline = time.monotonic() + timeout if timeout else None
    retried = False
    while True:
        while (remaining := _queue_cooldown_seconds(pr, repo)) > 0:
            if _deadline_hit(deadline, pr, timeout, "queue cooldown not finished"):
                return 1
            print(
                f"PR #{pr} head updated {remaining}s ago — queue cooldown, waiting",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(min(remaining, every))

        result = check_ci(pr, repo=repo)
        if result.verdict is CIStatus.ERROR:
            print(
                f"PR #{pr} CI error before queueing: {result.error_detail}",
                file=sys.stderr,
                flush=True,
            )
            return 3
        if result.verdict is not CIStatus.ALL_PASSED:
            print(f"PR #{pr} CI no longer green: {result.summary()}", file=sys.stderr, flush=True)
            return 1

        command = _queue_command(repo, result.head_sha)
        rc = _enqueue_pr(pr, repo, command)
        if rc != 0:
            return rc
        command_time = time.time()
        rc, rejected = _watch_enqueue(pr, repo, command_time, every, deadline, timeout)
        if not rejected:
            return rc
        if retried:
            print(
                "queue command rejected twice by Mergify — not enqueued",
                file=sys.stderr,
                flush=True,
            )
            return 4
        print(f"retrying queue command in {RETRY_BACKOFF_SECONDS}s", file=sys.stderr, flush=True)
        time.sleep(RETRY_BACKOFF_SECONDS)
        retried = True


def _wait_for_verdict(
    pr: str,
    repo: str,
    every: int,
    timeout: int,
    *,
    merge: bool,
    queue: str = "mergify",
    priority: str = "medium",
    trunk_token: str | None = None,
) -> int:
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
                if queue == "trunk":
                    if trunk_token is None:
                        raise AssertionError("Trunk merge flow requires a token")
                    return _trunk_merge_flow(
                        pr, repo, priority, every=every, timeout=timeout, token=trunk_token
                    )
                return _merge_flow(pr, repo, every, timeout)
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
    implies `--wait` and enqueues once green."""
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
        help="with --wait: enqueue the PR once CI is green and wait for the selected "
        "merge queue to land it, with head-update cooldown (implies --wait; "
        "default timeout 1800s)",
    )
    p.add_argument(
        "--queue",
        choices=_QUEUE_CHOICES,
        default=None,
        help="with --merge: merge queue (default: CI_QUEUE, else mergify)",
    )
    p.add_argument(
        "--priority",
        choices=tuple(_TRUNK_PRIORITIES),
        default="medium",
        help="with --queue trunk: queue priority (default: medium)",
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
    try:
        queue = _resolve_queue(args.queue)
    except ValueError as error:
        p.error(str(error))
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

    trunk_token = None
    if args.merge and queue == "trunk":
        trunk_token = os.environ.get("TRUNK_API_TOKEN")
        if not trunk_token:
            print(
                "TRUNK_API_TOKEN is required for --queue trunk --merge", file=sys.stderr, flush=True
            )
            return 3

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
        return _wait_for_verdict(
            args.pr,
            args.repo,
            args.every,
            args.timeout,
            merge=args.merge,
            queue=queue,
            priority=args.priority,
            trunk_token=trunk_token,
        )
    return _query_once(args.pr, args.repo, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
