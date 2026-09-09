"""Job-level re-run of failed GitHub Actions jobs (issue #102).

`gh run rerun --failed` is refused while any job of the run is still going
("This workflow is already running"), so run-level recovery waits on the
slowest surviving shard — the more shards, the more likely one is slow, and
the more likely you need the re-run exactly then. A failed job can be
re-run at job level (POST /actions/jobs/{id}/rerun) while its siblings keep
running, so recovery stops depending on unrelated jobs finishing.

Used by scripts/ci_utils.py (--rerun-failed-jobs); the polling helper there
already knows which checks are red, this module knows how to re-run them.

The job objects come from the REST API, whose shape differs from the GraphQL
check runs ci_utils reads: the numeric job identifier is `.id` (not
`.databaseId`) and conclusions are lowercase (`"failure"`, `"timed_out"`).
A GitHub query that fails raises `CiJobRerunError` instead of reading as
"no failed jobs" (issue #1945).
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

# Conclusions that mean "this job failed and is re-runnable". Kept in sync
# with ci_utils.FAILING (scripts/ci_utils.py); REST conclusions arrive
# lowercase, so the comparison normalizes them on read.
FAILING = frozenset(
    {
        "FAILURE",
        "CANCELLED",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
        "TIMED_OUT",
    }
)


class CiJobRerunError(RuntimeError):
    """A GitHub query needed to list failed jobs could not be answered."""


def _gh_api_json(args: list[str], what: str) -> Any:
    """One `gh api` call parsed as JSON.

    A non-zero gh exit or unparseable stdout raises `CiJobRerunError`: the
    caller must distinguish "the query failed" from "the query found nothing",
    never fold the first into the second (issue #1945).
    """
    r = subprocess.run(  # noqa: S603
        ["gh", "api", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise CiJobRerunError(f"gh api {what} failed: {(r.stderr or r.stdout).strip()}")
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError as error:
        raise CiJobRerunError(
            f"gh api {what} returned unparseable output: {(r.stdout or '').strip()[:200]}"
        ) from error


def _head_sha_of(pr: str | int, repo: str) -> str:
    """PR head commit sha; a failed or empty read raises `CiJobRerunError`."""
    r = subprocess.run(  # noqa: S603
        ["gh", "pr", "view", str(pr), "-R", repo, "--json", "headRefOid", "--jq", ".headRefOid"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise CiJobRerunError(f"gh pr view failed: {(r.stderr or r.stdout).strip()}")
    sha = r.stdout.strip()
    if not sha:
        raise CiJobRerunError(f"gh pr view returned no head commit for {pr}")
    return sha


def _current_run_ids(head_sha: str, repo: str) -> list[int]:
    """Run ids for the PR head — the newest run per workflow only.

    One head commit hosts several workflow runs (CI, QA reviews, re-runs). A
    failed job in a superseded run is stale: re-running it would surface a
    check whose newer run already settled it, so only the newest run per
    workflow name is consulted (issue #1945, "stale QA checks").
    """
    data = _gh_api_json(
        [f"repos/{repo}/actions/runs?head_sha={head_sha}&per_page=100"],
        "runs",
    )
    newest: dict[str, tuple[str, int]] = {}
    for run in data.get("workflow_runs") or []:
        if not isinstance(run, dict):
            continue
        run_id = run.get("id")
        name = run.get("name")
        created_at = run.get("created_at")
        if run_id is None or not isinstance(name, str) or created_at is None:
            continue
        if name not in newest or created_at > newest[name][0]:
            newest[name] = (created_at, run_id)
    return sorted(run_id for _, run_id in newest.values())


def _all_jobs(head_sha: str, repo: str) -> list[dict]:
    """Every completed-or-pending job of the head's current workflow runs.

    The projection keeps the REST job id (`.id`, the numeric identifier used by
    `/actions/jobs/{id}/rerun`) — never GraphQL's `.databaseId`, which REST job
    objects do not carry (issue #1945).
    """
    jobs: list[dict] = []
    for run_id in _current_run_ids(head_sha, repo):
        data = _gh_api_json(
            [f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100"],
            f"run {run_id} jobs",
        )
        for job in data.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            jobs.append(
                {
                    "name": job.get("name"),
                    "job_id": job.get("id"),
                    "run_id": job.get("run_id", run_id),
                    "conclusion": job.get("conclusion"),
                }
            )
    return jobs


def list_failed_jobs(pr: str | int, repo: str) -> list[dict]:
    """Completed-failed jobs across every current workflow run of the PR's head.

    A failed job can be re-run while sibling jobs of the same run are still
    going — the window where run-level recovery is blocked. REST conclusions
    are lowercase and null while running: normalize on read, so only completed
    failures (`failure`, `timed_out`, `cancelled`, ...) are listed, never
    successful siblings and never superseded QA checks. A failed GitHub query
    raises `CiJobRerunError`; an empty list means the query succeeded and found
    nothing to re-run.
    """
    sha = _head_sha_of(pr, repo)
    return [
        j
        for j in _all_jobs(sha, repo)
        if isinstance(j.get("conclusion"), str) and j["conclusion"].upper() in FAILING
    ]


def rerun_failed_jobs(pr: str | int, repo: str) -> tuple[list[dict], list[str]]:
    """Re-run every failed job at job level; return (re-ran, errors)."""
    reran: list[dict] = []
    errors: list[str] = []
    for job in list_failed_jobs(pr, repo):
        r = subprocess.run(  # noqa: S603
            ["gh", "api", "--method", "POST", f"repos/{repo}/actions/jobs/{job['job_id']}/rerun"],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode == 0:
            reran.append(job)
        else:
            errors.append(f"{job['name']}: {(r.stderr or r.stdout).strip()}")
    return reran, errors
