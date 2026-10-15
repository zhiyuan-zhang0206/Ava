"""Tests for scripts/ci/pull_requests/job_rerun.py — re-run of failed CI jobs.

The behavior this file locks in (issue #102, task #3764): GitHub refuses
re-runs while the containing run is still going, so jobs of a still-running
run are reported as `waiting` with the recovery action instead of a forwarded
403; a completed run re-runs a single failed job at job level and several
failed jobs with one atomic run-level call (a second job-level rerun of the
same run is refused while the first's new attempt runs, probed 2026-09-17).

Issue #1945 adds the REST-shape contract: responses are real-shaped
(`workflow_runs` / `jobs` payloads, numeric `.id`, lowercase conclusions) and
a failed GitHub query raises instead of reading as "no failed jobs".
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

import pytest

from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from scripts.ci.pull_requests import job_rerun as rerun

# Untyped fixtures and helper calls throughout: the call-site rules stay at warning for this file.
# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownMemberType = warning


class _R:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _fake_gh(responses: list[_R]) -> tuple[Host, list[list[str]]]:
    """Host stub that pops one canned response per call."""
    calls: list[list[str]] = []

    def _run(argv: Sequence[str], **_k: object) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        calls.append(argv)
        resp = responses.pop(0)
        return subprocess.CompletedProcess(argv, resp.returncode, resp.stdout, resp.stderr)

    return fake_host(_run), calls


def _sha_response() -> _R:
    return _R(stdout="abc123")


def _runs_response(*runs: dict) -> _R:
    """Real-shaped REST runs payload: `{"workflow_runs": [...]}`."""
    return _R(stdout=json.dumps({"total_count": len(runs), "workflow_runs": list(runs)}))


def _run(run_id: int, name: str, created_at: str, status: str = "completed") -> dict:
    return {"id": run_id, "name": name, "created_at": created_at, "status": status}


def _jobs_response(*jobs: dict) -> _R:
    """Real-shaped REST jobs payload: `{"jobs": [...]}`."""
    return _R(stdout=json.dumps({"total_count": len(jobs), "jobs": list(jobs)}))


def _job(
    name: str,
    job_id: int,
    run_id: int,
    conclusion: str | None,
) -> dict:
    """Real-shaped REST job object: numeric `.id`, lowercase conclusion."""
    return {
        "id": job_id,
        "name": name,
        "run_id": run_id,
        "conclusion": conclusion,
    }


_CI_RUN = _run(11, "CI", "2026-09-09T10:00:00Z")
_QA_RUN = _run(12, "qa-review-signal", "2026-09-09T10:01:00Z")


def test_list_failed_jobs_returns_only_failed() -> None:
    """REST conclusions are lowercase; only completed failures are returned —
    passing, skipped, and still-running siblings are not."""
    host, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(
                _job("backend (1/8)", 101, 11, "success"),
                _job("e2e shard (3/4)", 102, 11, "failure"),
                _job("e2e shard (4/4)", 103, 11, None),
                _job("qa-review", 104, 11, "skipped"),
            ),
        ]
    )
    jobs = rerun.list_failed_jobs(42, "owner/repo", host=host)
    assert [(j["name"], j["job_id"]) for j in jobs] == [("e2e shard (3/4)", 102)]
    assert jobs[0]["run_id"] == 11
    assert jobs[0]["run_status"] == "completed"


def test_list_failed_jobs_keeps_rest_numeric_id() -> None:
    """The rerun URL needs the REST numeric id (`.id`), not GraphQL's
    `.databaseId`, which REST job objects do not carry (issue #1945)."""
    host, calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(_job("ubuntu cold-offline", 101695155877, 11, "failure")),
            _R(stdout="{}"),
        ]
    )
    reran, waiting, errors = rerun.rerun_failed_jobs(42, "owner/repo", host=host)
    assert [j["job_id"] for j in reran] == [101695155877]
    assert waiting == []
    assert errors == []
    posts = [c for c in calls if "POST" in c]
    assert len(posts) == 1
    assert "actions/jobs/101695155877/rerun" in posts[0][-1]


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled"])
def test_list_failed_jobs_normalizes_each_failing_conclusion(conclusion: str) -> None:
    """Every lowercase failing conclusion is recognized (issue #1945)."""
    host, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(_job("shard", 102, 11, conclusion)),
        ]
    )
    jobs = rerun.list_failed_jobs(42, "owner/repo", host=host)
    assert [(j["job_id"], j["conclusion"]) for j in jobs] == [(102, conclusion)]


def test_list_failed_jobs_spans_multiple_workflows() -> None:
    """Distinct workflows on the head are each consulted once."""
    host, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN, _QA_RUN),
            _jobs_response(_job("backend", 101, 11, "failure")),
            _jobs_response(_job("qa-review", 102, 12, "failure")),
        ]
    )
    jobs = rerun.list_failed_jobs(42, "owner/repo", host=host)
    assert sorted(j["job_id"] for j in jobs) == [101, 102]


def test_list_failed_jobs_ignores_superseded_runs() -> None:
    """A failed job in an older run of the same workflow is stale — the newer
    run settled the check — so only the newest run per workflow is listed
    (issue #1945, stale QA checks)."""
    stale = _run(11, "CI", "2026-09-09T09:00:00Z")
    current = _run(13, "CI", "2026-09-09T11:00:00Z")
    host, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(stale, current),
            _jobs_response(_job("backend", 101, 13, "success")),
        ]
    )
    assert rerun.list_failed_jobs(42, "owner/repo", host=host) == []


def test_list_failed_jobs_raises_when_sha_query_fails() -> None:
    """A failed head-sha query is an error, never an empty list (issue #1945)."""
    host, _calls = _fake_gh([_R(returncode=1, stderr="gh: not found")])
    with pytest.raises(rerun.CiJobRerunError, match="not found"):
        rerun.list_failed_jobs(42, "owner/repo", host=host)


def test_list_failed_jobs_raises_when_runs_query_fails() -> None:
    host, _calls = _fake_gh(
        [
            _sha_response(),
            _R(returncode=1, stderr="gh: rate limited"),
        ]
    )
    with pytest.raises(rerun.CiJobRerunError, match="rate limited"):
        rerun.list_failed_jobs(42, "owner/repo", host=host)


def test_list_failed_jobs_raises_on_unparseable_runs_output() -> None:
    host, _calls = _fake_gh([_sha_response(), _R(stdout="not json")])
    with pytest.raises(rerun.CiJobRerunError, match="unparseable"):
        rerun.list_failed_jobs(42, "owner/repo", host=host)


def test_list_failed_jobs_raises_when_jobs_query_fails() -> None:
    host, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _R(returncode=1, stderr="gh: 502"),
        ]
    )
    with pytest.raises(rerun.CiJobRerunError, match="502"):
        rerun.list_failed_jobs(42, "owner/repo", host=host)


def test_list_failed_jobs_empty_is_only_a_successful_query() -> None:
    """A successful query with no failing jobs is the only empty result."""
    host, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(_job("backend", 101, 11, "success")),
        ]
    )
    assert rerun.list_failed_jobs(42, "owner/repo", host=host) == []


def test_rerun_failed_jobs_posts_one_rerun_per_failed_job() -> None:
    host, calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(
                _job("backend (1/8)", 101, 11, "success"),
                _job("e2e shard (3/4)", 102, 11, "failure"),
            ),
            _R(stdout="{}"),
        ]
    )
    reran, waiting, errors = rerun.rerun_failed_jobs(42, "owner/repo", host=host)
    assert [j["job_id"] for j in reran] == [102]
    assert waiting == []
    assert errors == []
    posts = [c for c in calls if "POST" in c]
    assert len(posts) == 1
    assert "actions/jobs/102/rerun" in posts[0][-1]


def test_rerun_failed_jobs_reports_rejected_jobs() -> None:
    """A rerun request gh rejects lands in errors, not in the re-ran list."""
    host, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(_job("lint", 104, 11, "failure")),
            _R(returncode=1, stderr="gh: rate limited"),
        ]
    )
    reran, waiting, errors = rerun.rerun_failed_jobs(42, "owner/repo", host=host)
    assert reran == [] and waiting == []
    assert any("lint" in e and "rate limited" in e for e in errors)


def test_rerun_failed_jobs_waits_on_a_still_running_run() -> None:
    """A failed job whose run is still going cannot be re-run yet (GitHub
    refuses both flavors with 403): it lands in `waiting` and no POST is even
    attempted, so the caller reports the recovery action (task #3764)."""
    still_running = _run(11, "CI", "2026-09-09T10:00:00Z", status="in_progress")
    host, calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(still_running),
            _jobs_response(
                _job("e2e shard (3/4)", 102, 11, "failure"),
                _job("backend", 101, 11, "success"),
            ),
        ]
    )
    reran, waiting, errors = rerun.rerun_failed_jobs(42, "owner/repo", host=host)
    assert reran == []
    assert errors == []
    assert [j["job_id"] for j in waiting] == [102]
    assert waiting[0]["run_status"] == "in_progress"
    assert not [c for c in calls if "POST" in c]


def test_rerun_failed_jobs_uses_run_level_for_multiple_failed_jobs() -> None:
    """Several failed jobs in one completed run re-run with one atomic
    run-level call: a second job-level rerun of the same run is refused while
    the first's new attempt runs (probed 2026-09-17, task #3764)."""
    host, calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(
                _job("backend (1/8)", 101, 11, "failure"),
                _job("e2e shard (3/4)", 102, 11, "failure"),
            ),
            _R(stdout="{}"),
        ]
    )
    reran, waiting, errors = rerun.rerun_failed_jobs(42, "owner/repo", host=host)
    assert [j["job_id"] for j in reran] == [101, 102]
    assert waiting == [] and errors == []
    posts = [c for c in calls if "POST" in c]
    assert len(posts) == 1
    assert "actions/runs/11/rerun-failed-jobs" in posts[0][-1]


def test_rerun_failed_jobs_reports_rejected_run_level_call() -> None:
    """A rejected run-level rerun request (the multi-failed-job path) lands in
    `errors` with the run and its job count — never in `reran` (task #3764)."""
    host, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(
                _job("backend (1/8)", 101, 11, "failure"),
                _job("e2e shard (3/4)", 102, 11, "failure"),
            ),
            _R(returncode=1, stderr="gh: rate limited"),
        ]
    )
    reran, waiting, errors = rerun.rerun_failed_jobs(42, "owner/repo", host=host)
    assert reran == [] and waiting == []
    assert any("run 11" in e and "2 failed jobs" in e and "rate limited" in e for e in errors)
