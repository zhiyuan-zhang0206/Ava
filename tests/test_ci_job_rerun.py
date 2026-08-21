"""Tests for scripts/ci_job_rerun.py — job-level re-run of failed CI jobs.

The behavior this file locks in (issue #102): recovery must not be gated on
the slowest surviving shard. `gh run rerun --failed` is refused while any job
of the run is still going, so the re-run picks the failed jobs individually
(POST /actions/jobs/{id}/rerun) instead of re-running the whole run.
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


def _runs_response(*run_ids: int) -> _R:
    return _R(stdout=json.dumps(list(run_ids)))


def _jobs_response(*jobs: dict) -> _R:
    return _R(stdout=json.dumps(list(jobs)))


def _job(name: str, job_id: int, run_id: int, conclusion: str | None) -> dict:
    return {"name": name, "job_id": job_id, "run_id": run_id, "conclusion": conclusion}


def test_list_failed_jobs_returns_only_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Jobs with a failing conclusion are returned; passing / in-progress ones are not."""
    fake, _calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(11),
            _jobs_response(
                _job("backend (1/8)", 101, 11, "SUCCESS"),
                _job("e2e shard (3/4)", 102, 11, "FAILURE"),
                _job("e2e shard (4/4)", 103, 11, None),  # still in progress
                _job("lint", 104, 11, "TIMED_OUT"),
            ),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    jobs = rerun.list_failed_jobs(42, "owner/repo")
    assert [j["job_id"] for j in jobs] == [102, 104]


def test_list_failed_jobs_spans_multiple_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PR head can have several workflow runs; failed jobs from all of them count."""
    fake, _ = _fake_gh(
        [
            _sha_response(),
            _runs_response(11, 12),
            _jobs_response(_job("a", 101, 11, "FAILURE")),
            _jobs_response(_job("b", 201, 12, "CANCELLED")),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    jobs = rerun.list_failed_jobs(42, "owner/repo")
    assert [j["job_id"] for j in jobs] == [101, 201]


def test_list_failed_jobs_empty_when_gh_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A gh failure (pr view non-zero) reads as nothing to re-run, not a crash."""
    fake, _ = _fake_gh([_R(returncode=1, stderr="gh: not authenticated")])
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    assert rerun.list_failed_jobs(42, "owner/repo") == []


def test_list_failed_jobs_skips_unparseable_jobs_query(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = _fake_gh(
        [
            _sha_response(),
            _runs_response(11, 12),
            _R(stdout="not json"),
            _jobs_response(_job("b", 201, 12, "FAILURE")),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    jobs = rerun.list_failed_jobs(42, "owner/repo")
    assert [j["job_id"] for j in jobs] == [201]


def test_rerun_failed_jobs_posts_one_rerun_per_failed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each failed job gets its own POST /actions/jobs/{id}/rerun — no run-level gate."""
    fake, calls = _fake_gh(
        [
            _sha_response(),
            _runs_response(11),
            _jobs_response(
                _job("e2e shard (3/4)", 102, 11, "FAILURE"),
                _job("backend (1/8)", 101, 11, "SUCCESS"),
            ),
            _R(stdout="", returncode=0),
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
    fake, _ = _fake_gh(
        [
            _sha_response(),
            _runs_response(11),
            _jobs_response(_job("lint", 104, 11, "FAILURE")),
            _R(returncode=1, stderr="gh: rate limited"),
        ]
    )
    monkeypatch.setattr(rerun.subprocess, "run", fake)
    reran, errors = rerun.rerun_failed_jobs(42, "owner/repo")
    assert reran == []
    assert any("lint" in e and "rate limited" in e for e in errors)
