from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from scripts.ci.pull_requests import accounting

SINCE = "2026-09-06T08:00:00Z"
UNTIL = "2026-09-06T12:00:00Z"


def _run(run_id: int = 101, **changes: Any) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": "CI",
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-09-06T10:00:00Z",
        "head_branch": "codex/work",
        "head_commit": {"message": "fix(cli): improve parser"},
        "pull_requests": [],
        **changes,
    }


def _job(job_id: int = 1, **changes: Any) -> dict[str, Any]:
    return {
        "id": job_id,
        "name": "backend",
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-09-06T10:00:00Z",
        "completed_at": "2026-09-06T10:04:00Z",
        **changes,
    }


def _page(key: str, rows: list[dict[str, Any]], total: int | None = None) -> str:
    return json.dumps({"total_count": len(rows) if total is None else total, key: rows})


def _host(responses: list[tuple[str, str | Exception]], calls: list[list[str]]) -> Host:
    def run(argv: Sequence[str], **_: Any) -> subprocess.CompletedProcess[str]:
        args = list(argv)
        calls.append(args)
        joined = " ".join(args)
        for index, (key, answer) in enumerate(responses):
            if key in joined:
                responses.pop(index)
                if isinstance(answer, Exception):
                    raise answer
                return subprocess.CompletedProcess(args, 0, answer, "")
        return subprocess.CompletedProcess(args, 1, "", "unmatched request")

    return fake_host(run)


def _collect(host: Host) -> list[dict[str, Any]]:
    return accounting.collect("o/r", SINCE, UNTIL, host=host)


def test_actual_github_envelope_retains_pr_identity() -> None:
    calls: list[list[str]] = []
    host = _host(
        [
            ("actions/runs?", _page("workflow_runs", [_run(pull_requests=[{"number": 42}])])),
            ("pr view 42 ", "[Ava-5811] improve parser (task #2571)"),
            ("runs/101/jobs?", _page("jobs", [_job()])),
        ],
        calls,
    )
    [entry] = _collect(host)
    assert (entry["pr_number"], entry["agent_id"], entry["task_id"]) == (42, 5811, 2571)
    assert entry["linux_minutes"] == 4
    assert all("--jq" not in args for args in calls if args[1] == "api")


def test_run_pagination_uses_raw_pages_before_filtering_ci() -> None:
    calls: list[list[str]] = []
    other_runs = [_run(index, name="lint") for index in range(100)]
    host = _host(
        [
            ("actions/runs?per_page=100&page=1", _page("workflow_runs", other_runs, 101)),
            ("actions/runs?per_page=100&page=2", _page("workflow_runs", [_run()], 101)),
            ("runs/101/jobs?", _page("jobs", [_job()])),
        ],
        calls,
    )
    assert [entry["run_id"] for entry in _collect(host)] == [101]
    assert any("page=2" in args[2] for args in calls)


def test_jobs_pagination_counts_every_completed_job() -> None:
    host = _host(
        [
            ("actions/runs?", _page("workflow_runs", [_run()])),
            ("jobs?per_page=100&page=1", _page("jobs", [_job(i) for i in range(100)], 101)),
            ("jobs?per_page=100&page=2", _page("jobs", [_job(100)], 101)),
        ],
        [],
    )
    [entry] = _collect(host)
    assert (entry["jobs"], entry["linux_minutes"]) == (101, 404)


@pytest.mark.parametrize(
    "answer",
    [subprocess.TimeoutExpired("gh", 60), "not json", _page("jobs", [], 1)],
)
def test_failed_or_incomplete_jobs_never_append_a_zero_entry(
    answer: str | Exception, tmp_path: Path
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    existing = '{"run_id":99,"linux_minutes":5}\n'
    ledger.write_text(existing)
    host = _host(
        [
            ("actions/runs?", _page("workflow_runs", [_run()])),
            ("runs/101/jobs?", answer),
        ],
        [],
    )
    result = accounting.main(
        ["--repo", "o/r", "--since", SINCE, "--until", UNTIL, "--append-ledger", str(ledger)],
        host=host,
    )
    assert result == 1
    assert ledger.read_text() == existing


@pytest.mark.parametrize("unfinished", ["run", "job", "timestamp"])
def test_unfinished_evidence_cannot_become_a_final_zero(unfinished: str) -> None:
    run = _run(status="in_progress") if unfinished == "run" else _run()
    job = _job(completed_at=None)
    if unfinished == "job":
        job["status"] = "in_progress"
    host = _host(
        [
            ("actions/runs?", _page("workflow_runs", [run])),
            ("runs/101/jobs?", _page("jobs", [job])),
        ],
        [],
    )
    with pytest.raises(RuntimeError, match=r"incomplete|completed"):
        _collect(host)


@pytest.mark.parametrize("conclusion", ["cancelled", "skipped"])
def test_completed_never_started_jobs_keep_legitimate_zero(conclusion: str) -> None:
    host = _host(
        [
            ("actions/runs?", _page("workflow_runs", [_run(conclusion=conclusion)])),
            (
                "runs/101/jobs?",
                _page("jobs", [_job(conclusion=conclusion, started_at=None, completed_at=None)]),
            ),
        ],
        [],
    )
    [entry] = _collect(host)
    assert (entry["jobs"], entry["linux_minutes"], entry["macos_minutes"]) == (1, 0, 0)


def test_successful_empty_window_remains_empty() -> None:
    assert _collect(_host([("actions/runs?", _page("workflow_runs", []))], [])) == []


def test_later_run_failure_does_not_append_an_earlier_complete_run(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    host = _host(
        [
            ("actions/runs?", _page("workflow_runs", [_run(101), _run(102)])),
            ("runs/101/jobs?", _page("jobs", [_job()])),
            ("runs/102/jobs?", subprocess.TimeoutExpired("gh", 60)),
        ],
        [],
    )
    assert (
        accounting.main(
            ["--repo", "o/r", "--since", SINCE, "--until", UNTIL, "--append-ledger", str(ledger)],
            host=host,
        )
        == 1
    )
    assert not ledger.exists()


def test_completed_run_without_jobs_is_a_complete_zero() -> None:
    host = _host(
        [
            ("actions/runs?", _page("workflow_runs", [_run(conclusion="cancelled")])),
            ("runs/101/jobs?", _page("jobs", [])),
        ],
        [],
    )
    [entry] = _collect(host)
    assert (entry["jobs"], entry["linux_minutes"], entry["macos_minutes"]) == (0, 0, 0)


@pytest.mark.parametrize("total", [1000, 1001])
def test_runs_over_github_search_limit_require_a_narrower_window(total: int) -> None:
    host = _host([("actions/runs?", _page("workflow_runs", [_run()], total))], [])
    with pytest.raises(accounting.CiUsageReadError, match="narrow the window"):
        _collect(host)


@pytest.mark.parametrize("failure", ["count", "duplicate"])
def test_changing_or_duplicate_pages_refuse_incomplete_evidence(failure: str) -> None:
    first = [_run(index, name="lint") for index in range(100)]
    second = [_run(0 if failure == "duplicate" else 101)]
    host = _host(
        [
            ("actions/runs?per_page=100&page=1", _page("workflow_runs", first, 101)),
            (
                "actions/runs?per_page=100&page=2",
                _page("workflow_runs", second, 102 if failure == "count" else 101),
            ),
        ],
        [],
    )
    with pytest.raises(accounting.CiUsageReadError, match="incomplete"):
        _collect(host)


def test_exclusive_until_does_not_finalize_the_next_window_run() -> None:
    run = _run(created_at=UNTIL, status="in_progress", conclusion=None)
    assert _collect(_host([("actions/runs?", _page("workflow_runs", [run]))], [])) == []


def test_pr_title_failure_is_not_an_unattributed_success() -> None:
    host = _host(
        [("actions/runs?", _page("workflow_runs", [_run(pull_requests=[{"number": 42}])]))], []
    )
    with pytest.raises(accounting.CiUsageReadError, match=r"PR 42 title.*exit 1"):
        _collect(host)


@pytest.mark.parametrize("completed_at", ["2026-09-06T09:59:00Z", ""])
def test_invalid_job_timestamps_are_not_a_valid_zero(completed_at: str) -> None:
    host = _host(
        [
            ("actions/runs?", _page("workflow_runs", [_run()])),
            ("runs/101/jobs?", _page("jobs", [_job(completed_at=completed_at)])),
        ],
        [],
    )
    with pytest.raises(accounting.CiUsageReadError, match="timestamps"):
        _collect(host)


def test_missing_start_evidence_is_not_a_never_started_job() -> None:
    job = _job(conclusion="cancelled", completed_at=None)
    del job["started_at"]
    host = _host(
        [
            ("actions/runs?", _page("workflow_runs", [_run()])),
            ("runs/101/jobs?", _page("jobs", [job])),
        ],
        [],
    )
    with pytest.raises(accounting.CiUsageReadError, match="timestamps"):
        _collect(host)


@pytest.mark.parametrize("invalid", ["missing", "null", "integer", "empty", "whitespace"])
def test_invalid_workflow_name_cannot_append_a_partial_window(invalid: str, tmp_path: Path) -> None:
    malformed = _run(
        102, name={"null": None, "integer": 17, "empty": "", "whitespace": "  "}.get(invalid)
    )
    if invalid == "missing":
        del malformed["name"]
    ledger = tmp_path / "ledger.jsonl"
    existing = '{"run_id":99,"linux_minutes":5}\n'
    ledger.write_text(existing)
    host = _host(
        [
            ("actions/runs?", _page("workflow_runs", [_run(), malformed])),
            ("runs/101/jobs?", _page("jobs", [_job()])),
        ],
        [],
    )
    assert (
        accounting.main(
            ["--repo", "o/r", "--since", SINCE, "--until", UNTIL, "--append-ledger", str(ledger)],
            host=host,
        )
        == 1
    )
    assert ledger.read_text() == existing


def test_missing_workflow_name_is_not_a_known_empty_window() -> None:
    host = _host([("actions/runs?", _page("workflow_runs", [{"id": 101}]))], [])
    with pytest.raises(accounting.CiUsageReadError, match="workflow name"):
        _collect(host)
