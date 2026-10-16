from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any

from base.host.net.ports import UrlOpener
from base.host.system.hostport import Host
from scripts.ci.pull_requests import trunk_api
from scripts.ci.pull_requests.job_rerun import FAILING, CiJobRerunError, list_failed_jobs

_GH_TIMEOUT_S = 30.0
_DIAGNOSE_LOG_TAIL = 4000
_DIAGNOSE_MAX_JOB_LOGS = 8
_DIAGNOSE_MAX_SYNTHETIC_PRS = 3

# ava:ref scripts/ci/pull_requests/docs/diagnostics.ava.okf.md#failure-signature-table
_FAILURE_SIGNATURES: list[tuple[str, str, str]] = [
    (
        r"over the 800-line hard ceiling",
        "lint hard limit (file over 800 lines)",
        "split the file into focused modules, fix, resubmit",
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


# ava:ref scripts/ci/pull_requests/docs/diagnostics.ava.okf.md#best-effort-evidence
def _pr_view(pr: str, repo: str, fields: str, *, host: Host) -> dict[str, object] | None:
    try:
        result = host.run(
            ["gh", "pr", "view", pr, "--repo", repo, "--json", fields],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _job_log_tail(job_id: int, repo: str, *, host: Host) -> str:
    try:
        result = host.run(
            ["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout[-_DIAGNOSE_LOG_TAIL:]


def _classify_check(name: str, log: str) -> tuple[str, str]:
    for pattern, label, action in _FAILURE_SIGNATURES:
        if re.search(pattern, log, re.IGNORECASE):
            return label, action
    if "lint" in name.lower():
        return "deterministic lint failure", "fix the offending code/comment, resubmit"
    return "unclassified CI failure", "read the log tail; rerun once if it looks environmental"


def _pull_head_ref(pull: dict[str, object]) -> str:
    head = pull.get("head")
    if not isinstance(head, dict):
        return ""
    return str(head.get("ref") or "")


def _synthetic_test_prs(pr: str, repo: str, *, host: Host) -> list[dict[str, object]]:
    try:
        result = host.run(
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
            timeout=_GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return []
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


# ava:ref scripts/ci/pull_requests/docs/diagnostics.ava.okf.md#pr-level-issues
def _pr_level_issues(
    view: dict[str, object], pr: str, repo: str, *, host: Host
) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    mergeable = view.get("mergeable")
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
        stale, unreadable = trunk_api.base_freshness(pr, repo, host=host)
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


def _failing_check_entries(
    view: dict[str, object], pr: str, repo: str, *, host: Host
) -> list[dict[str, object]]:
    try:
        failed_jobs = list_failed_jobs(pr, repo, host=host)
    except CiJobRerunError:
        failed_jobs = []
    raw_rollup = view.get("statusCheckRollup", [])
    rollup_rows = raw_rollup if isinstance(raw_rollup, list) else []
    rollup = [c for c in rollup_rows if isinstance(c, dict)]
    failing_checks = [c for c in rollup if c.get("conclusion") in FAILING][:_DIAGNOSE_MAX_JOB_LOGS]
    entries: list[dict[str, object]] = []
    for check in failing_checks:
        check_name = str(check.get("name") or "unnamed check")
        job = next((j for j in failed_jobs if j.get("name") == check_name), None)
        log = _job_log_tail(int(job["job_id"]), repo, host=host) if job else ""
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


def _trunk_diagnosis(pr: str, repo: str, token: str, *, open_url: UrlOpener) -> dict[str, object]:
    data, error = trunk_api.post(
        "getSubmittedPullRequest", trunk_api.pr_payload(pr, repo), token, open_url=open_url
    )
    if error is None and data is not None:
        return {
            "state": data.get("state"),
            "reason": data.get("reason"),
            "readiness": data.get("readiness"),
            "verifiedByTestRun": data.get("verifiedByTestRun"),
        }
    return {"error": error}


def diagnose_pr(
    pr: str,
    repo: str,
    *,
    token: str | None,
    host: Host,
    open_url: UrlOpener,
) -> dict[str, Any]:
    diag: dict[str, Any] = {
        "pr": pr,
        "repo": repo,
        "issues": [],
        "checks": [],
        "trunk": None,
        "synthetic_test_prs": [],
    }
    view = _pr_view(pr, repo, "mergeable,headRefOid,state,statusCheckRollup", host=host)
    if view is None:
        diag["gh_error"] = "gh pr view failed"
        return diag
    diag["state"] = view.get("state")
    diag["mergeable"] = view.get("mergeable")
    diag["issues"] = _pr_level_issues(view, pr, repo, host=host)
    diag["checks"] = _failing_check_entries(view, pr, repo, host=host)
    if token:
        diag["trunk"] = _trunk_diagnosis(pr, repo, token, open_url=open_url)
    diag["synthetic_test_prs"] = [
        {
            "number": pull.get("number"),
            "state": pull.get("state"),
            "head_ref": _pull_head_ref(pull),
        }
        for pull in _synthetic_test_prs(pr, repo, host=host)
    ]
    return diag


def print_diagnosis(diag: dict[str, Any]) -> None:
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
