"""Re-run of failed GitHub Actions jobs on a PR's current workflow runs (issue #102).

GitHub refuses both re-run flavors while the containing run is still going, so
there is no early window to recover a flaky job in:

- job level, `POST /repos/{repo}/actions/jobs/{id}/rerun` -> 403 "The workflow
  run containing this job is already running";
- run level, `POST /repos/{repo}/actions/runs/{id}/rerun-failed-jobs` (and
  `.../rerun`) -> 403 "This workflow is already running".

Probed live 2026-09-17 (task #3764): the Actions re-run how-to states the
30-day window and no docs page states this precondition; the API refuses
until the run reads `completed`. The recovery for a still-running run is
"wait for the run to finish, then re-run", which `rerun_failed_jobs` reports
as `waiting` instead of forwarding the 403.

Once a run is completed, `rerun_failed_jobs` re-runs its failed jobs: one
job-level rerun when a single job failed (the narrowest intervention), and one
run-level `rerun-failed-jobs` call when several did — a second job-level rerun
of the same run is refused while the first rerun's new attempt runs (probed
2026-09-17), so multi-job recovery is atomic at run level, the same policy
.github/workflows/ci-rerun.yml applies.

Used by scripts/ci_utils.py (--rerun-failed-jobs). The job objects come from
the REST API, whose shape differs from the GraphQL check runs ci_utils reads:
the numeric job identifier is `.id` (not `.databaseId`) and conclusions are
lowercase (`"failure"`, `"timed_out"`). A GitHub query that fails raises
`CiJobRerunError` instead of reading as "no failed jobs" (issue #1945).
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


def _current_runs(head_sha: str, repo: str) -> list[dict]:
    """The newest run per workflow name for the PR head, with its REST status.

    One head commit hosts several workflow runs (CI, QA reviews, re-runs). A
    failed job in a superseded run is stale: re-running it would surface a
    check whose newer run already settled it, so only the newest run per
    workflow name is consulted (issue #1945, "stale QA checks"). The run's
    `status` rides along — a re-run is refused until it reads `completed`
    (task #3764).
    """
    data = _gh_api_json(
        [f"repos/{repo}/actions/runs?head_sha={head_sha}&per_page=100"],
        "runs",
    )
    newest: dict[str, tuple[str, int, str]] = {}
    for run in data.get("workflow_runs") or []:
        if not isinstance(run, dict):
            continue
        run_id = run.get("id")
        name = run.get("name")
        created_at = run.get("created_at")
        status = run.get("status")
        if (
            run_id is None
            or not isinstance(name, str)
            or created_at is None
            or not isinstance(status, str)
        ):
            continue
        if name not in newest or created_at > newest[name][0]:
            newest[name] = (created_at, run_id, status)
    runs = [{"run_id": run_id, "status": status} for _, run_id, status in newest.values()]
    runs.sort(key=lambda run: run["run_id"])
    return runs


def _all_jobs(head_sha: str, repo: str) -> list[dict]:
    """Every completed-or-pending job of the head's current workflow runs.

    The projection keeps the REST job id (`.id`, the numeric identifier used by
    `/actions/jobs/{id}/rerun`) — never GraphQL's `.databaseId`, which REST job
    objects do not carry (issue #1945) — and the containing run's `status` and
    `run_id`, on which re-run eligibility turns (task #3764).
    """
    jobs: list[dict] = []
    for run in _current_runs(head_sha, repo):
        data = _gh_api_json(
            [f"repos/{repo}/actions/runs/{run['run_id']}/jobs?per_page=100"],
            f"run {run['run_id']} jobs",
        )
        for job in data.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            jobs.append(
                {
                    "name": job.get("name"),
                    "job_id": job.get("id"),
                    "run_id": job.get("run_id", run["run_id"]),
                    "conclusion": job.get("conclusion"),
                    "run_status": run["status"],
                }
            )
    return jobs


def list_failed_jobs(pr: str | int, repo: str) -> list[dict]:
    """Completed-failed jobs across every current workflow run of the PR's head.

    Each entry carries the job's REST id and conclusion plus the containing
    run's `run_id` and `run_status`; only completed failures (`failure`,
    `timed_out`, `cancelled`, ...) are listed, never successful siblings and
    never superseded QA checks. A failed GitHub query raises `CiJobRerunError`;
    an empty list means the query succeeded and found nothing to re-run.
    """
    sha = _head_sha_of(pr, repo)
    return [
        j
        for j in _all_jobs(sha, repo)
        if isinstance(j.get("conclusion"), str) and j["conclusion"].upper() in FAILING
    ]


def rerun_failed_jobs(pr: str | int, repo: str) -> tuple[list[dict], list[dict], list[str]]:
    """Re-run the failed jobs of the PR head's current runs.

    Returns `(reran, waiting, errors)`: jobs whose re-run was accepted; jobs
    that cannot be re-run yet because their run is still going — GitHub refuses
    both flavors until the run reads `completed` (probed 2026-09-17, task
    #3764), so the caller reports the recovery action instead of the 403 — and
    re-run requests GitHub rejected. A completed run with a single failed job
    re-runs at job level; several failed jobs re-run with one run-level
    `rerun-failed-jobs` call, because a second job-level rerun of the same run
    is refused while the first's new attempt runs.
    """
    reran: list[dict] = []
    waiting: list[dict] = []
    errors: list[str] = []
    by_run: dict[int, list[dict]] = {}
    for job in list_failed_jobs(pr, repo):
        by_run.setdefault(job["run_id"], []).append(job)
    for run_id, jobs in by_run.items():
        if jobs[0]["run_status"] != "completed":
            waiting.extend(jobs)
            continue
        if len(jobs) == 1:
            r = subprocess.run(  # noqa: S603
                [
                    "gh",
                    "api",
                    "--method",
                    "POST",
                    f"repos/{repo}/actions/jobs/{jobs[0]['job_id']}/rerun",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if r.returncode == 0:
                reran.append(jobs[0])
            else:
                errors.append(f"{jobs[0]['name']}: {(r.stderr or r.stdout).strip()}")
            continue
        r = subprocess.run(  # noqa: S603
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode == 0:
            reran.extend(jobs)
        else:
            errors.append(
                f"run {run_id} ({len(jobs)} failed jobs): {(r.stderr or r.stdout).strip()}"
            )
    return reran, waiting, errors
