"""Job-level re-run of failed GitHub Actions jobs (issue #102).

`gh run rerun --failed` is refused while any job of the run is still going
("This workflow is already running"), so run-level recovery waits on the
slowest surviving shard — the more shards, the more likely one is slow, and
the more likely you need the re-run exactly then. A failed job can be
re-run at job level (POST /actions/jobs/{id}/rerun) while its siblings keep
running, so recovery stops depending on unrelated jobs finishing.

Used by scripts/ci_utils.py (--rerun-failed-jobs); the polling helper there
already knows which checks are red, this module knows how to re-run them.
"""

from __future__ import annotations

import json
import subprocess

# Conclusions that mean "this job failed and is re-runnable". Kept in sync
# with ci_utils.FAILING (scripts/ci_utils.py).
FAILING = frozenset(
    {
        "FAILURE",
        "CANCELLED",
        "ACTION_REQUIRED",
        "STARTUP_FAILURE",
        "TIMED_OUT",
    }
)


def _head_sha_of(pr: str | int, repo: str) -> str:
    """PR head commit sha; empty string when gh fails."""
    r = subprocess.run(  # noqa: S603
        ["gh", "pr", "view", str(pr), "-R", repo, "--json", "headRefOid", "--jq", ".headRefOid"],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _all_jobs(head_sha: str, repo: str) -> list[dict]:
    """Every job of every workflow run for `head_sha` (across all workflows).

    Best-effort: a failing runs/jobs query contributes no jobs, so a gh hiccup
    reads as "nothing to re-run" rather than a crash.
    """
    jobs: list[dict] = []
    r = subprocess.run(  # noqa: S603
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runs?head_sha={head_sha}&per_page=100",
            "--jq",
            "[.workflow_runs[] | .id]",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        run_ids = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []
    for run_id in run_ids:
        j = subprocess.run(  # noqa: S603
            [
                "gh",
                "api",
                f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100",
                "--jq",
                "[.jobs[] | {name, job_id: .databaseId, run_id, conclusion}]",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            jobs.extend(json.loads(j.stdout or "[]"))
        except json.JSONDecodeError:
            continue
    return jobs


def list_failed_jobs(pr: str | int, repo: str) -> list[dict]:
    """Completed-failed jobs across every workflow run of the PR's head.

    A failed job can be re-run while sibling jobs of the same run are still
    going — the window where run-level recovery is blocked.
    """
    sha = _head_sha_of(pr, repo)
    if not sha:
        return []
    return [j for j in _all_jobs(sha, repo) if j.get("conclusion") in FAILING]


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
