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
    persistent gh/network errors, 4 Trunk queue submission failed.
    `--merge` implies `--wait`
    and submits the PR to Trunk once green. `--require-fresh-base` turns the
    advisory base-staleness warning (main advanced past the PR's base) into a
    refusal, so the operator rebases instead of paying an in-queue re-test. `--queue` (or `CI_QUEUE`) selects
    the Trunk queue; `--priority` maps the submission priority, which requires
    `TRUNK_API_TOKEN`. Trunk refuses submission unless the PR has the
    `qa-approved` label, and `.trunk/trunk.yaml` requires its
    `qa-approved-gate` status. Submission waits at least five minutes after a
    head update. The all-green predicate excludes the "Trunk Merge Queue"
    queue-state check and "qa-approved-gate". This is the canonical CI watcher: launch it with
    ava.shell.run_background, and the completion notice delivers the exit code
    + verdict to the agent automatically.
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

# The Ava checkout root this script ships in — anchors base-freshness git reads
# against THIS repo's origin regardless of the caller's cwd (task #2496).
_REPO_ROOT = Path(__file__).resolve().parent.parent


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
_QUEUE_CHOICES = ("trunk",)
_QUEUE_NAMES = frozenset(_QUEUE_CHOICES)
_TRUNK_PRIORITIES = {"urgent": 0, "high": 10, "medium": 100, "low": 200}
_TRUNK_API_BASE_URL = "https://api.trunk.io/v1"
_TRUNK_REQUEST_TIMEOUT_SECONDS = 30


def _resolve_queue(queue: str | None) -> str:
    """Return the CLI-selected queue, then CI_QUEUE, then the Trunk default."""
    resolved = queue or os.environ.get("CI_QUEUE") or "trunk"
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
    # Trunk's queue-state check is likewise not a CI result and must not turn
    # an otherwise green PR into a perpetual PENDING verdict.
    trunk_checks: list[dict] = field(default_factory=list)
    # The QA gate and evidence evaluator are required by Trunk and checked before
    # its submission, not by this CI verdict. Keep them separate so they cannot
    # enter the verdict buckets.
    gate_checks: list[dict] = field(default_factory=list)
    mergeable: str = ""  # MERGEABLE / CONFLICTING / UNKNOWN
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


TRUNK_MERGE_QUEUE_CHECK_NAME = "Trunk Merge Queue"
QA_APPROVED_GATE_CHECK_NAME = "qa-approved-gate"
QA_EVIDENCE_CHECK_NAME = "evaluate-qa-evidence"
# Both are QA evidence produced by the qa-approved-gate workflow and enforced by
# the queue before submission — never CI conclusions, so they never enter the
# verdict buckets.
QA_GATE_CHECK_NAMES = frozenset({QA_APPROVED_GATE_CHECK_NAME, QA_EVIDENCE_CHECK_NAME})


def _latest_completed_per_name(checks: list[dict]) -> list[dict]:
    """Keep the newest COMPLETED check run per name.

    GitHub lists every check run on a commit, but branch protection and the
    required-status UI treat same-named runs as ONE logical check whose state
    is the newest COMPLETED run's. A stale CANCELLED run on the same SHA must
    not poison the verdict when a later run of the same name succeeded — the
    QA evaluator's cancel-in-progress concurrency produces exactly this shape
    (2026-09-04: two CANCELLED evaluate-qa-evidence runs froze #1636).

    Commit statuses are exempt: GitHub already deduplicates StatusContext by
    context, and they carry `state`, not `status`/`conclusion`."""
    latest: dict[str, dict] = {}
    for c in checks:
        name = c.get("name")
        if not isinstance(name, str) or c.get("__typename") == "StatusContext":
            continue
        if c.get("status") != "COMPLETED":
            continue
        current = latest.get(name)
        if current is None:
            latest[name] = c
            continue
        if (_parse_ts(c.get("completedAt")) or 0) >= (_parse_ts(current.get("completedAt")) or 0):
            latest[name] = c
    kept: list[dict] = []
    for c in checks:
        name = c.get("name")
        if (
            c.get("__typename") == "StatusContext"
            or not isinstance(name, str)
            or c.get("status") != "COMPLETED"
            or latest.get(name) is c
        ):
            kept.append(c)
    return kept


def _partition_checks(checks: list[dict], result: CIResult) -> None:
    """Sort each rollup check into completed / passed / failed / pending on
    `result`, and record which of them a workflow produced.

    Only COMPLETED checks are judged: a QUEUED / IN_PROGRESS one is pending, and
    so is a COMPLETED one whose conclusion is unrecognized — guessing there is
    how a false "all green" gets reported.

    Trunk checks report queue state rather than CI results, while the
    `qa-approved-gate` and `evaluate-qa-evidence` checks are required by Trunk
    and checked before submission. All are routed to dedicated buckets so they
    never enter the verdict fields.
    """
    for c in checks:
        if c.get("__typename") == "StatusContext":
            # Commit statuses (qa_gate.py publishes qa-approved-gate this way)
            # have `context` + `state`, not `name` + `status` + `conclusion`.
            # Without this branch they read as a nameless "?" entry with a
            # null status — an eternal PENDING that froze every --wait watcher
            # (2026-09-04: five PRs stalled with all real checks green).
            context = c.get("context", "?")
            state = c.get("state", "")
            if context in QA_GATE_CHECK_NAMES:
                result.gate_checks.append(c)
                continue
            if state == "SUCCESS":
                result.completed.append(context)
                result.passed.append(context)
            elif state in ("FAILURE", "ERROR"):
                result.completed.append(context)
                result.failed.append({"name": context, "conclusion": state})
            else:
                result.pending.append(context)
            continue
        name = c.get("name", "?")
        status = c.get("status", "")
        conclusion = c.get("conclusion", "")

        if name.startswith(TRUNK_MERGE_QUEUE_CHECK_NAME):
            result.trunk_checks.append(c)
            continue
        if name in QA_GATE_CHECK_NAMES:
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

    # --- Merge conflict detection ---
    result.mergeable = data.get("mergeable", "UNKNOWN")
    if result.mergeable in CONFLICTING:
        result.verdict = CIStatus.MERGE_CONFLICT
        # Still populate checks for visibility
        checks = data.get("statusCheckRollup", [])
        result.checks = checks
        for c in checks:
            name = c.get("name", "?")
            if name in QA_GATE_CHECK_NAMES:
                result.gate_checks.append(c)
                continue
            status = c.get("status", "")
            if status == "COMPLETED":
                result.completed.append(name)
            else:
                result.pending.append(name)
        return result

    # --- CI check evaluation ---
    checks = _latest_completed_per_name(data.get("statusCheckRollup", []))
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
                    "trunk_checks": result.trunk_checks,
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


def _trunk_pr_payload(pr: str, repo: str) -> dict[str, object]:
    """Build the repository and PR identity required by Trunk's API."""
    owner, name = repo.split("/", maxsplit=1)
    return {
        "repo": {"host": "github.com", "owner": owner, "name": name},
        "pr": {"number": int(pr)},
        "targetBranch": "main",
    }


def _trunk_qa_approved(pr: str, repo: str) -> tuple[bool, str | None]:
    """Return whether a PR has the required QA label, or its read error."""
    result = subprocess.run(  # noqa: S603
        ["gh", "pr", "view", pr, "--repo", repo, "--json", "labels"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False, f"gh labels error: {result.stderr.strip()}"
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return False, f"labels JSON error: {error}"
    labels = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(labels, list):
        return False, "labels response was not a list"
    return any(
        isinstance(label, dict) and label.get("name") == "qa-approved" for label in labels
    ), None


_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _is_sha(value: str) -> bool:
    return bool(_SHA1_RE.match(value) or _SHA256_RE.match(value))


def _base_freshness(pr: str, repo: str) -> tuple[tuple[str, str] | None, bool]:
    """Return ((base_sha, main_sha) when stale else None, unreadable).

    The PR's `baseRefOid` is the base-branch SHA GitHub last evaluated the PR
    against. When current main is ahead of it, the queue's predictive branch
    will include commits this PR's green CI never saw, so Trunk re-tests the
    tree against the newer base — the extra in-queue round task #2496 (A1)
    wants operators warned about. Advisory only: any read error or non-SHA
    output sets `unreadable` and never blocks submission.
    """
    result = subprocess.run(  # noqa: S603
        ["gh", "pr", "view", pr, "--repo", repo, "--json", "baseRefOid", "--jq", ".baseRefOid"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None, True
    base_sha = result.stdout.strip()
    if not _is_sha(base_sha):
        return None, True
    result = subprocess.run(  # noqa: S603
        # -C anchors the checkout: cwd can be another git repo (e.g. the memory
        # pool), whose origin would answer with a VALID sha and mis-refuse in
        # require mode (QA NIT, 2026-09-06).
        ["git", "-C", str(_REPO_ROOT), "ls-remote", "origin", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None, True
    main_sha = result.stdout.split(maxsplit=1)[0]
    if not _is_sha(main_sha):
        return None, True
    if base_sha == main_sha:
        return None, False
    return (base_sha, main_sha), False


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
    except urllib.error.HTTPError as error:
        # urllib raises HTTPError (a URLError subclass) instead of returning
        # the response for 4xx/5xx, and its str() is "HTTP Error 409: ..." —
        # normalize to "HTTP <code>" so callers can match statuses exactly.
        return None, f"HTTP {error.code}"
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
        if error == "HTTP 409":
            print(
                f"PR #{pr} already in the Trunk merge queue — resuming watch",
                file=sys.stderr,
                flush=True,
            )
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
    """Poll Trunk's submitted-PR state until it merges, fails, or times out.

    Terminal states: "merged", "failed", "cancelled". Everything else —
    including "pending" (waiting for a batch), "not_ready" (required statuses
    not yet green), and "testing" (merge-tree test run in progress, observed
    live 2026-09-03) — is non-terminal: keep polling.
    """
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
                print(
                    f"PR #{pr} Trunk queue {state}: {reason} — "
                    f"full getSubmittedPullRequest payload: {json.dumps(data, indent=2)}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            print(f"[ci] Trunk queue state: {state or 'unknown'}", file=sys.stderr, flush=True)
        if _deadline_hit(deadline, pr, timeout, "still in the Trunk merge queue"):
            return 1
        time.sleep(every)


def _trunk_merge_flow(
    pr: str,
    repo: str,
    priority: str,
    *,
    every: int,
    timeout: int,
    token: str,
    require_fresh_base: bool = False,
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

    qa_approved, label_error = _trunk_qa_approved(pr, repo)
    if label_error is not None:
        print(
            f"PR #{pr} could not verify qa-approved label: {label_error}",
            file=sys.stderr,
            flush=True,
        )
        return 3
    if not qa_approved:
        print(
            f"PR #{pr} lacks the qa-approved label — not submitting to Trunk",
            file=sys.stderr,
            flush=True,
        )
        return 1

    stale, unreadable = _base_freshness(pr, repo)
    if stale is not None:
        base_sha, main_sha = stale
        print(
            f"PR #{pr} base {base_sha[:8]} lags main {main_sha[:8]} — main advanced since this "
            "PR last synced, so Trunk will re-test against the newer base (an extra "
            "in-queue round). Rebase onto main before submitting to skip it.",
            file=sys.stderr,
            flush=True,
        )
        if require_fresh_base:
            print(
                f"PR #{pr} not submitted: --require-fresh-base is set",
                file=sys.stderr,
                flush=True,
            )
            return 1
    elif unreadable and require_fresh_base:
        print(
            f"PR #{pr} could not verify base freshness — submitting anyway "
            "(advisory check fails open)",
            file=sys.stderr,
            flush=True,
        )

    rc = _submit_trunk(pr, repo, priority, token=token)
    if rc != 0:
        return rc
    return _watch_trunk_enqueue(
        pr, repo, every=every, deadline=deadline, timeout=timeout, token=token
    )


def _wait_for_verdict(
    pr: str,
    repo: str,
    every: int,
    timeout: int,
    *,
    merge: bool,
    priority: str = "medium",
    trunk_token: str | None = None,
    require_fresh_base: bool = False,
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
                if trunk_token is None:
                    raise AssertionError("Trunk merge flow requires a token")
                return _trunk_merge_flow(
                    pr,
                    repo,
                    priority,
                    every=every,
                    timeout=timeout,
                    token=trunk_token,
                    require_fresh_base=require_fresh_base,
                )
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
    implies `--wait` and submits to Trunk once green."""
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
        help="with --merge: merge queue (default: CI_QUEUE, else trunk)",
    )
    p.add_argument(
        "--priority",
        choices=tuple(_TRUNK_PRIORITIES),
        default="medium",
        help="with --queue trunk: queue priority (default: medium)",
    )
    p.add_argument(
        "--require-fresh-base",
        action="store_true",
        help="with --merge: refuse to submit when main has advanced past the PR's "
        "recorded base (the queue would re-test against the newer base)",
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
        _resolve_queue(args.queue)
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
    if args.merge:
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
            priority=args.priority,
            trunk_token=trunk_token,
            require_fresh_base=args.require_fresh_base,
        )
    return _query_once(args.pr, args.repo, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
