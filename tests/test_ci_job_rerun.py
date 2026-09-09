"""Tests for scripts/ci_job_rerun.py — job-level re-run of failed CI jobs.

The behavior this file locks in (issue #102): recovery must not be gated on
the slowest surviving shard. `gh run rerun --failed` is refused while any job
of the run is still going, so the re-run picks the failed jobs individually
(POST /actions/jobs/{id}/rerun) instead of re-running the whole run.

Issue #1945 adds the REST-shape contract: responses are real-shaped
(`workflow_runs` / `jobs` payloads, numeric `.id`, lowercase conclusions) and
a failed GitHub query raises instead of reading as "no failed jobs".
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ci_job_rerun.py"
_MOD_NAME = "ci_job_rerun_under_test"
_spec = importlib.util.spec_from_file_location(_MOD_NAME, _MOD_PATH)
assert _spec and _spec.loader
rerun = importlib.util.module_from_spec(_spec)
sys.modules[_MOD_NAME] = rerun
_spec.loader.exec_module(rerun)


class _R:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _fake_gh(responses: list[Any]) -> Any:
    """subprocess.run stub that pops one canned response per call."""
    calls: list[list[str]] = []

    def _run(cmd: list[str], **_k) -> _R:
        calls.append(cmd)
        return responses.pop(0)

    return _run, calls


def _sha_response() -> _R:
    return _R(stdout="abc123")


def _runs_response(*runs: dict) -> _R:
    """Real-shaped REST runs payload: `{"workflow_runs": [...]}`."""
    return _R(stdout=json.dumps({"total_count": len(runs), "workflow_runs": list(runs)}))


def _run(run_id: int, name: str, created_at: str) -> dict:
    return {"id": run_id, "name": name, "created_at": created_at}


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


def test_list_failed_jobs_returns_only_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """REST conclusions are lowercase; only completed failures are returned —
    passing, skipped, and still-running siblings are not."""
    fake, _calls = _fake_gh(
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
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    jobs = rerun.list_failed_jobs(42, "owner/repo")
    assert [(j["name"], j["job_id"]) for j in jobs] == [("e2e shard (3/4)", 102)]
    assert jobs[0]["run_id"] == 11


def test_list_failed_jobs_keeps_rest_numeric_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rerun URL needs the REST numeric id (`.id`), not GraphQL's
    `.databaseId`, which REST job objects do not carry (issue #1945)."""
    fake, calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(_job("ubuntu cold-offline", 101695155877, 11, "failure")),
            _R(stdout="{}"),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    reran, errors = rerun.rerun_failed_jobs(42, "owner/repo")
    assert [j["job_id"] for j in reran] == [101695155877]
    assert errors == []
    posts = [c for c in calls if "POST" in c]
    assert len(posts) == 1
    assert "actions/jobs/101695155877/rerun" in posts[0][-1]


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled"])
def test_list_failed_jobs_normalizes_each_failing_conclusion(
    monkeypatch: pytest.MonkeyPatch, conclusion: str
) -> None:
    """Every lowercase failing conclusion is recognized (issue #1945)."""
    fake, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(_job("shard", 102, 11, conclusion)),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    jobs = rerun.list_failed_jobs(42, "owner/repo")
    assert [(j["job_id"], j["conclusion"]) for j in jobs] == [(102, conclusion)]


def test_list_failed_jobs_spans_multiple_workflows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Distinct workflows on the head are each consulted once."""
    fake, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN, _QA_RUN),
            _jobs_response(_job("backend", 101, 11, "failure")),
            _jobs_response(_job("qa-review", 102, 12, "failure")),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    jobs = rerun.list_failed_jobs(42, "owner/repo")
    assert sorted(j["job_id"] for j in jobs) == [101, 102]


def test_list_failed_jobs_ignores_superseded_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed job in an older run of the same workflow is stale — the newer
    run settled the check — so only the newest run per workflow is listed
    (issue #1945, stale QA checks)."""
    stale = _run(11, "CI", "2026-09-09T09:00:00Z")
    current = _run(13, "CI", "2026-09-09T11:00:00Z")
    fake, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(stale, current),
            _jobs_response(_job("backend", 101, 13, "success")),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    assert rerun.list_failed_jobs(42, "owner/repo") == []


def test_list_failed_jobs_raises_when_sha_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed head-sha query is an error, never an empty list (issue #1945)."""
    fake, _calls = _fake_gh([_R(returncode=1, stderr="gh: not found")])
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    with pytest.raises(rerun.CiJobRerunError, match="not found"):
        rerun.list_failed_jobs(42, "owner/repo")


def test_list_failed_jobs_raises_when_runs_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _calls = _fake_gh(
        [
            _sha_response(),
            _R(returncode=1, stderr="gh: rate limited"),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    with pytest.raises(rerun.CiJobRerunError, match="rate limited"):
        rerun.list_failed_jobs(42, "owner/repo")


def test_list_failed_jobs_raises_on_unparseable_runs_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, _calls = _fake_gh([_sha_response(), _R(stdout="not json")])
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    with pytest.raises(rerun.CiJobRerunError, match="unparseable"):
        rerun.list_failed_jobs(42, "owner/repo")


def test_list_failed_jobs_raises_when_jobs_query_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _R(returncode=1, stderr="gh: 502"),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    with pytest.raises(rerun.CiJobRerunError, match="502"):
        rerun.list_failed_jobs(42, "owner/repo")


def test_list_failed_jobs_empty_is_only_a_successful_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful query with no failing jobs is the only empty result."""
    fake, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(_job("backend", 101, 11, "success")),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    assert rerun.list_failed_jobs(42, "owner/repo") == []


def test_rerun_failed_jobs_posts_one_rerun_per_failed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, calls = _fake_gh(
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
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    reran, errors = rerun.rerun_failed_jobs(42, "owner/repo")
    assert [j["job_id"] for j in reran] == [102]
    assert errors == []
    posts = [c for c in calls if "POST" in c]
    assert len(posts) == 1
    assert "actions/jobs/102/rerun" in posts[0][-1]


def test_rerun_failed_jobs_reports_rejected_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rerun request gh rejects lands in errors, not in the re-ran list."""
    fake, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(_CI_RUN),
            _jobs_response(_job("lint", 104, 11, "failure")),
            _R(returncode=1, stderr="gh: rate limited"),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    reran, errors = rerun.rerun_failed_jobs(42, "owner/repo")
    assert reran == []
    assert any("lint" in e and "rate limited" in e for e in errors)
