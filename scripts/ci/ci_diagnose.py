"""`ci_utils.py --diagnose`: classify a PR's failing CI checks from rollup, job logs and Trunk state."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any

from scripts.ci import trunk_api
from scripts.ci.job_rerun import FAILING, CiJobRerunError, list_failed_jobs

_DIAGNOSE_LOG_TAIL = 4000
_DIAGNOSE_MAX_JOB_LOGS = 8
_DIAGNOSE_MAX_SYNTHETIC_PRS = 3

# (regex, classification, suggested action) — first match wins.
_FAILURE_SIGNATURES: list[tuple[str, str, str]] = [
    (
        r"over the 800-line hard ceiling",
        "lint hard limit (file over 800 lines)",
        "split the file into focused modules, fix, resubmit",
    ),
    (
        r"First Load JS shared by all",
        "frontend first-load JavaScript budget",
        "reduce the bundle or update the budget baseline, fix, resubmit",
    ),
    (
        r"toMatchImageSnapshot|visual regression|baseline image|snapshot baseline",
        "e2e visual regression (stale snapshot baseline)",
        "UI change: refresh the visual snapshot baseline, resubmit",
    ),
    (
        r"archive cache is empty|no offline fallback|apt-get install",
        "runner-side network flake (pgdg/apt family)",
        "rerun the failed job — no code change; resubmit if the queue already failed",
    ),
    (
        r"truncate-isolation|comment-stripping regex",
        "deterministic truncate-isolation lint failure",
        "fix the triggering comment/word, resubmit",
    ),
    (
        r"lattice-vocabulary|clock lattice",
        "deterministic clock-lattice lint failure (constant outside its family module)",
        "move the constant into the family module, fix, resubmit",
    ),
]


def _pr_view(pr: str, repo: str, fields: str) -> dict[str, object] | None:
    """gh pr view --json; None when gh fails or the output is not an object."""
    result = subprocess.run(  # noqa: S603
        ["gh", "pr", "view", pr, "--repo", repo, "--json", fields],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _job_log_tail(job_id: int, repo: str) -> str:
    """The last `_DIAGNOSE_LOG_TAIL` chars of a job log; empty when unreadable."""
    result = subprocess.run(  # noqa: S603
        ["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout[-_DIAGNOSE_LOG_TAIL:]


def _classify_check(name: str, log: str) -> tuple[str, str]:
    """(classification, suggested action) from a failing check's native log."""
    for pattern, label, action in _FAILURE_SIGNATURES:
        if re.search(pattern, log, re.IGNORECASE):
            return label, action
    if "lint" in name.lower():
        return "deterministic lint failure", "fix the offending code/comment, resubmit"
    return "unclassified CI failure", "read the log tail; rerun once if it looks environmental"


def _pull_head_ref(pull: dict[str, object]) -> str:
    """The head branch ref of a pull payload; empty when unreadable."""
    head = pull.get("head")
    if not isinstance(head, dict):
        return ""
    return str(head.get("ref") or "")


def _synthetic_test_prs(pr: str, repo: str) -> list[dict[str, object]]:
    """Trunk's trunk-merge/pr-<n>/* test PRs (each = one queue attempt)."""
    result = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            f"repos/{repo}/pulls",
            "-X",
            "GET",
            "-f",
            "state=all",
            "-f",
            "per_page=100",
            "-f",
            "sort=updated",
            "-f",
            "direction=desc",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        pulls = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    prefix = f"trunk-merge/pr-{pr}/"
    return [
        pull for pull in pulls if isinstance(pull, dict) and _pull_head_ref(pull).startswith(prefix)
    ][:_DIAGNOSE_MAX_SYNTHETIC_PRS]


def _pr_level_issues(view: dict[str, object], pr: str, repo: str) -> list[dict[str, object]]:
    """Merge-conflict and base-freshness issues of an open PR (merged/closed PRs have none)."""
    issues: list[dict[str, object]] = []
    mergeable = view.get("mergeable")
    # A merged/closed PR reports mergeable=UNKNOWN and its recorded base is
    # naturally behind main — those are not diagnosable problems.
    if view.get("state") == "OPEN" and mergeable == "CONFLICTING":
        issues.append(
            {
                "kind": "merge_conflict",
                "detail": f"mergeable={mergeable}",
                "action": "rebase on origin/main, resubmit",
            }
        )
    stale = None
    unreadable = False
    if view.get("state") == "OPEN":
        stale, unreadable = trunk_api.base_freshness(pr, repo)
    if stale:
        issues.append(
            {
                "kind": "stale_base",
                "detail": f"base {stale[0][:8]} vs main {stale[1][:8]}",
                "action": "rebase, or drop --require-fresh-base and let the queue re-test the new base",
            }
        )
    elif unreadable:
        issues.append(
            {
                "kind": "base_unreadable",
                "detail": "base freshness could not be read",
                "action": "verify the PR base against current main manually",
            }
        )
    return issues


def _failing_check_entries(view: dict[str, object], pr: str, repo: str) -> list[dict[str, object]]:
    """Classified entries (with job log tails) for the PR's failing checks."""
    try:
        failed_jobs = list_failed_jobs(pr, repo)
    except CiJobRerunError:
        # Diagnosis stays best-effort: the rollup still names the failing
        # checks; only the per-job log tail enrichment degrades.
        failed_jobs = []
    raw_rollup = view.get("statusCheckRollup", [])
    rollup_rows = raw_rollup if isinstance(raw_rollup, list) else []
    rollup = [c for c in rollup_rows if isinstance(c, dict)]
    failing_checks = [c for c in rollup if c.get("conclusion") in FAILING][:_DIAGNOSE_MAX_JOB_LOGS]
    entries: list[dict[str, object]] = []
    for check in failing_checks:
        check_name = str(check.get("name") or "unnamed check")
        job = next((j for j in failed_jobs if j.get("name") == check_name), None)
        log = _job_log_tail(int(job["job_id"]), repo) if job else ""
        classification, action = _classify_check(check_name, log)
        entry: dict[str, object] = {
            "check": check_name,
            "conclusion": check.get("conclusion"),
            "classification": classification,
            "action": action,
            "log_tail": log[-800:],
        }
        if job:
            entry["job_id"] = job["job_id"]
        entries.append(entry)
    return entries


def _trunk_diagnosis(pr: str, repo: str, token: str) -> dict[str, object]:
    data, error = trunk_api.post("getSubmittedPullRequest", trunk_api.pr_payload(pr, repo), token)
    if error is None and data is not None:
        return {
            "state": data.get("state"),
            "reason": data.get("reason"),
            "readiness": data.get("readiness"),
            "verifiedByTestRun": data.get("verifiedByTestRun"),
        }
    return {"error": error}


def diagnose_pr(pr: str, repo: str, *, token: str | None) -> dict[str, Any]:
    """Collect the PR's failure evidence and classify it (task #2572).
    Diagnosis only — no repair action is taken here; the operator executes the suggested action.
    Evidence sources: the PR's check rollup + job log tails, Trunk's queue state, and
    the synthetic trunk-merge test PRs.
    """
    diag: dict[str, Any] = {
        "pr": pr,
        "repo": repo,
        "issues": [],
        "checks": [],
        "trunk": None,
        "synthetic_test_prs": [],
    }
    view = _pr_view(pr, repo, "mergeable,headRefOid,state,statusCheckRollup")
    if view is None:
        diag["gh_error"] = "gh pr view failed"
        return diag
    diag["state"] = view.get("state")
    diag["mergeable"] = view.get("mergeable")
    diag["issues"] = _pr_level_issues(view, pr, repo)
    diag["checks"] = _failing_check_entries(view, pr, repo)
    if token:
        diag["trunk"] = _trunk_diagnosis(pr, repo, token)
    diag["synthetic_test_prs"] = [
        {
            "number": pull.get("number"),
            "state": pull.get("state"),
            "head_ref": _pull_head_ref(pull),
        }
        for pull in _synthetic_test_prs(pr, repo)
    ]
    return diag


def print_diagnosis(diag: dict[str, Any]) -> None:
    """Human-readable --diagnose report."""
    if diag.get("gh_error"):
        print(f"PR #{diag['pr']} diagnosis failed: {diag['gh_error']}", file=sys.stderr)
        return
    print(
        f"PR #{diag['pr']} diagnosis (state={diag.get('state')}, mergeable={diag.get('mergeable')})"
    )
    issues = diag.get("issues", [])
    if not issues:
        print("No PR-level issues found.")
    for issue in issues:
        print(f"- {issue['kind']}: {issue['detail']} → {issue['action']}")
    checks = diag.get("checks", [])
    if not checks:
        print("No failing checks on the PR head.")
    for check in checks:
        print(f"  check {check['check']}: {check['classification']} → {check['action']}")
    trunk = diag.get("trunk")
    if isinstance(trunk, dict):
        if trunk.get("error"):
            print(f"Trunk queue: not queryable ({trunk['error']})")
        else:
            print(f"Trunk queue: state={trunk.get('state')} reason={trunk.get('reason')}")
    synthetic = diag.get("synthetic_test_prs", [])
    for pull in synthetic:
        print(
            f"  synthetic test PR #{pull.get('number')} [{pull.get('state')}] "
            f"{pull.get('head_ref')}"
        )
    if synthetic:
        print(
            "  → queue-attempt failures: inspect the synthetic PR's failed job log "
            "(runner-side flake families rerun without code change)"
        )
