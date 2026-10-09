# pyright: reportUnknownArgumentType = warning
# pyright: reportUnknownMemberType = warning
"""Ci monitor cases: runs probe reads only incomplete runs."""

from __future__ import annotations

import email.message
import json
import subprocess
import urllib.error
import urllib.request
from typing import Any, cast

import pytest

from scripts.ci.pull_requests import commands as ci_utils
from scripts.ci.pull_requests import owner_operations, status
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    _APP_CHECK,
    CIStatus,
    _check,
    _completed,
    _core_rollup,
    _gh_runner,
    _TrunkResponse,
    _urlopen_sequence,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    diag_gh as diag_gh,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    gh as gh,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    has_workflows as has_workflows,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    no_sleep as no_sleep,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    poll as poll,
)


def test_runs_probe_reads_only_incomplete_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The jq the probe sends filters on `status != completed` — a finished run
    that produced no check is not evidence of one still coming."""
    seen: dict[str, list[str]] = {}

    class _R:
        returncode = 0
        stdout = '["CI"]'
        stderr = ""

    def _run(cmd, *_a, **_k):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(status.subprocess, "run", _run)
    assert status._runs_not_yet_reporting("abc123", None) == ["CI"]
    joined = " ".join(seen["cmd"])
    assert "head_sha=abc123" in joined
    assert 'select(.status != "completed")' in joined


@pytest.mark.parametrize("returncode, stdout", [(1, ""), (0, "not json")])
def test_runs_probe_returns_none_on_bad_response(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    """A failed query is None (unanswerable), not [] (nothing scheduled) — the
    distinction is what keeps an unanswerable probe from reading as green."""
    response = subprocess.CompletedProcess([], returncode, stdout, "boom")
    monkeypatch.setattr(status.subprocess, "run", lambda *_a, **_k: response)
    assert status._runs_not_yet_reporting("abc123", None) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-24T22:36:05Z", 1787610965.0),
        ('"2026-08-24T22:36:05Z"', 1787610965.0),
        ("null", None),
        ("bad", None),
    ],
)
def test_parse_ts(value: str | None, expected: float | None) -> None:
    assert status._parse_ts(value) == expected


@pytest.mark.parametrize(
    ("last", "now", "expected"), [(90.0, 100.0, 290), (0.0, 400.0, 0), (None, 100.0, 0)]
)
def test_queue_cooldown_seconds(
    monkeypatch: pytest.MonkeyPatch, last: float | None, now: float, expected: int
) -> None:
    monkeypatch.setattr(owner_operations, "_last_head_update", lambda *_a: last)
    monkeypatch.setattr(status.time, "time", lambda: now)
    assert owner_operations._queue_cooldown_seconds("7", "o/r") == expected


@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        (
            [_completed('"2026-08-24T22:36:05Z"'), _completed('"2026-08-24T22:35:00Z"')],
            1787610965.0,
        ),
        ([_completed(returncode=1), _completed('"2026-08-24T22:36:05Z"')], 1787610965.0),
        ([_completed(returncode=1), _completed(returncode=1)], None),
        ([_completed("null"), _completed("null")], None),
    ],
)
def test_last_head_update(
    monkeypatch: pytest.MonkeyPatch, replies: list[Any], expected: float | None
) -> None:
    reply_iter = iter(replies)
    seen: list[list[str]] = []
    monkeypatch.setattr(
        status.subprocess, "run", lambda cmd, **_k: seen.append(cmd) or next(reply_iter)
    )
    assert owner_operations._last_head_update("7", "o/r") == expected
    assert "/issues/7/timeline" in " ".join(seen[0])
    assert "/pulls/7/commits" in " ".join(seen[1])


def test_wait_all_passed_exits_zero(no_sleep, poll, capsys) -> None:
    poll(CIStatus.ALL_PASSED)
    assert ci_utils.main(["1243", "--wait"]) == 0
    assert "CI green" in capsys.readouterr().out


@pytest.mark.parametrize(
    "verdict",
    [CIStatus.FAILED, CIStatus.MERGE_CONFLICT, CIStatus.NO_WORKFLOW_RUNS, CIStatus.NOT_READY],
)
def test_wait_not_green_exits_one(no_sleep, poll, capsys, verdict) -> None:
    poll(verdict)
    assert ci_utils.main(["1243", "--wait"]) == 1
    assert "NOT green" in capsys.readouterr().err


def test_wait_not_ready_names_draft_gating(no_sleep, poll, capsys) -> None:
    poll(CIStatus.NOT_READY)
    assert ci_utils.main(["1243", "--wait"]) == 1
    assert "draft gating" in capsys.readouterr().err


def test_wait_failed_lists_failed_checks(no_sleep, poll, capsys) -> None:
    poll(CIStatus.FAILED)
    assert ci_utils.main(["1243", "--wait"]) == 1
    assert "lint" in capsys.readouterr().err


def test_wait_pending_then_green(no_sleep, poll) -> None:
    poll(CIStatus.PENDING, CIStatus.PENDING, CIStatus.ALL_PASSED)
    assert ci_utils.main(["1243", "--wait"]) == 0


def test_wait_no_checks_then_green(no_sleep, poll, capsys) -> None:
    poll(CIStatus.NO_CHECKS, CIStatus.ALL_PASSED)
    assert ci_utils.main(["1243", "--wait"]) == 0
    assert "no checks yet" in capsys.readouterr().err


def test_wait_transient_error_then_green(no_sleep, poll) -> None:
    # One gh/network hiccup must not kill the poll — only 3 consecutive do.
    poll(CIStatus.ERROR, CIStatus.PENDING, CIStatus.ALL_PASSED)
    assert ci_utils.main(["1243", "--wait"]) == 0


def test_wait_three_consecutive_errors_exit_three(no_sleep, poll, capsys) -> None:
    poll(CIStatus.ERROR)
    assert ci_utils.main(["1243", "--wait"]) == 3
    assert "boom" in capsys.readouterr().err


def test_wait_timeout_while_pending_exits_one(monkeypatch, poll, capsys) -> None:
    # --timeout bounds the wait for run_background use (no watchdog there);
    # a still-pending PR at the deadline is a failed watch, not a silent hang.
    poll(CIStatus.PENDING)
    monkeypatch.setattr(status.time, "sleep", lambda _s: None)
    assert ci_utils.main(["1243", "--wait", "--timeout", "1"]) == 1
    assert "timed out" in capsys.readouterr().err


def test_wait_merge_trunk_submits_and_lands_when_green(no_sleep, poll, monkeypatch, capsys) -> None:
    poll(CIStatus.ALL_PASSED)
    gh_calls: list[list[str]] = []
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(owner_operations, "_queue_cooldown_seconds", lambda *_a, **_k: 0)
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(status.subprocess, "run", _gh_runner(gh_calls))
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _urlopen_sequence(
            [
                _TrunkResponse({"accepted": True}),
                _TrunkResponse({"state": "pending"}),
                _TrunkResponse({"state": "merged"}),
            ],
            requests,
        ),
    )
    # --merge implies --wait: with the Trunk default queue, submit the PR
    # then wait for the queue to land it.
    assert ci_utils.main(["1243", "--merge"]) == 0
    assert requests[0].full_url == "https://api.trunk.io/v1/submitPullRequest"
    assert requests[0].get_header("X-api-token") == "test-token"
    assert requests[0].data is not None
    assert json.loads(cast(bytes, requests[0].data)) == {
        "repo": {"host": "github.com", "owner": "zhiyuan-zhang0206", "name": "Ava"},
        "pr": {"number": 1243},
        "targetBranch": "main",
        "priority": 100,
        "noBatch": False,
    }
    assert requests[1].full_url == "https://api.trunk.io/v1/getSubmittedPullRequest"
    assert "PR #1243 merged by the Trunk merge queue" in capsys.readouterr().out
    assert gh_calls[0][:4] == ["gh", "pr", "view", "1243"]


def test_wait_merge_trunk_failed_state_prints_full_payload(
    no_sleep, poll, monkeypatch, capsys
) -> None:
    """Task #2541: a failed/cancelled Trunk submission often carries an empty
    `reason`, so the old one-line print was undiagnosable (five first-submit
    failures 2026-09-06 left no trace). The full getSubmittedPullRequest
    payload must be printed on the terminal branch."""
    poll(CIStatus.ALL_PASSED)
    monkeypatch.setattr(owner_operations, "_queue_cooldown_seconds", lambda *_a, **_k: 0)
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(status.subprocess, "run", _gh_runner([]))
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _urlopen_sequence(
            [
                _TrunkResponse({"accepted": True}),
                _TrunkResponse({"state": "failed", "details": {"message": "batch rejected"}}),
            ],
            [],
        ),
    )
    assert ci_utils.main(["1243", "--merge"]) == 1
    err = capsys.readouterr().err
    assert "PR #1243 Trunk queue failed" in err
    assert "full getSubmittedPullRequest payload" in err
    assert '"state": "failed"' in err
    assert '"batch rejected"' in err


def test_wait_merge_trunk_submit_failure_exits_four(no_sleep, poll, monkeypatch, capsys) -> None:
    poll(CIStatus.ALL_PASSED)
    monkeypatch.setattr(owner_operations, "_queue_cooldown_seconds", lambda *_a, **_k: 0)
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(status.subprocess, "run", _gh_runner([]))
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _urlopen_sequence(
            [
                urllib.error.URLError("network down"),
                urllib.error.URLError("network down"),
            ],
            [],
        ),
    )
    # Two failed submit attempts (retried once) -> exit 4.
    assert ci_utils.main(["1243", "--merge"]) == 4
    assert "Trunk queue submission failed" in capsys.readouterr().err


def test_wait_usage_errors_exit_two() -> None:
    with pytest.raises(SystemExit) as e:
        ci_utils.main([])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        ci_utils.main(["1243", "--wait", "--every", "0"])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        ci_utils.main(["1243", "--wait", "--timeout", "-5"])
    assert e.value.code == 2


def test_wait_json_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as e:
        ci_utils.main(["1243", "--wait", "--json"])
    assert e.value.code == 2


def test_one_shot_behavior_unchanged(gh: Any, has_workflows: Any, capsys) -> None:
    """Legacy contract: without --wait, a PENDING probe prints and exits 0 —
    a one-shot query, not a poller."""
    gh([_APP_CHECK, _check("backend", "", status="IN_PROGRESS")])
    has_workflows(True)
    assert ci_utils.main(["1243"]) == 0
    out = capsys.readouterr()
    assert "pending" in out.out.lower()


def test_one_shot_not_ready_exits_one(gh: Any, has_workflows: Any, capsys) -> None:
    gh(_core_rollup("SKIPPED"), is_draft=True, main_completed=True)
    has_workflows(True)
    assert ci_utils.main(["1243"]) == 1
    assert "NOT READY" in capsys.readouterr().out


@pytest.mark.parametrize(
    "verdict",
    [
        CIStatus.ALL_PASSED,
        CIStatus.FAILED,
        CIStatus.NOT_READY,
        CIStatus.MERGE_CONFLICT,
        CIStatus.NO_CHECKS,
        CIStatus.NO_WORKFLOW_RUNS,
        CIStatus.ERROR,
    ],
)
def test_settled_verdicts_are_terminal(verdict: CIStatus) -> None:
    assert verdict.is_terminal


def test_pending_is_not_terminal() -> None:
    assert not CIStatus.PENDING.is_terminal


def test_json_probe_carries_terminal_flag(gh: Any, has_workflows: Any, capsys) -> None:
    """The --json one-shot output includes `terminal`, so a subprocess-based
    watcher can decide without re-deriving the verdict domain."""
    gh([_APP_CHECK, _check("backend (pytest + pyright)", "SUCCESS")])
    has_workflows(True)
    assert ci_utils.main(["1243", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "all_passed"
    assert payload["terminal"] is True
    assert "gate_checks" not in payload


def test_json_probe_reports_draft_and_core_skipped(gh: Any, has_workflows: Any, capsys) -> None:
    gh(_core_rollup("SKIPPED"), is_draft=True, main_completed=True)
    has_workflows(True)
    assert ci_utils.main(["1243", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "not_ready"
    assert payload["terminal"] is True
    assert payload["is_draft"] is True
    assert payload["core_skipped"]


def test_json_probe_pending_is_not_terminal(gh: Any, has_workflows: Any, capsys) -> None:
    gh([_APP_CHECK, _check("backend", "", status="IN_PROGRESS")])
    has_workflows(True)
    assert ci_utils.main(["1243", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "pending"
    assert payload["terminal"] is False


def test_rerun_failed_jobs_dry_run_lists_jobs(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """--dry-run lists the failed jobs and exits 0 without re-running anything."""
    monkeypatch.setattr(
        owner_operations,
        "list_failed_jobs",
        lambda _pr, _repo: [
            {
                "name": "e2e shard (3/4)",
                "job_id": 102,
                "run_id": 11,
                "conclusion": "FAILURE",
                "run_status": "completed",
            }
        ],
    )
    assert ci_utils.main(["42", "--rerun-failed-jobs", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "e2e shard (3/4)" in out and "102" in out and "[run completed]" in out


def test_rerun_failed_jobs_nothing_to_do(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(owner_operations, "list_failed_jobs", lambda _pr, _repo: [])
    assert ci_utils.main(["42", "--rerun-failed-jobs", "--dry-run"]) == 0
    assert "No failed jobs" in capsys.readouterr().out


def test_rerun_failed_jobs_forwards_and_reports_errors(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A rejected rerun request is printed and flips the exit code to 3."""
    monkeypatch.setattr(
        owner_operations,
        "rerun_failed_jobs",
        lambda _pr, _repo: ([], [], ["lint: gh: rate limited"]),
    )
    assert ci_utils.main(["42", "--rerun-failed-jobs"]) == 3
    assert "Re-run failed: lint: gh: rate limited" in capsys.readouterr().out


def test_rerun_failed_jobs_success_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        owner_operations,
        "rerun_failed_jobs",
        lambda _pr, _repo: (
            [{"name": "lint", "job_id": 104, "run_id": 11, "conclusion": "FAILURE"}],
            [],
            [],
        ),
    )
    assert ci_utils.main(["42", "--rerun-failed-jobs"]) == 0
    assert "Re-ran lint" in capsys.readouterr().out


def test_rerun_failed_jobs_waits_for_still_running_run(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Failures waiting on a still-running run are reported with the recovery
    action and exit 5, never forwarded as a 403 (task #3764)."""
    monkeypatch.setattr(
        owner_operations,
        "rerun_failed_jobs",
        lambda _pr, _repo: (
            [],
            [
                {
                    "name": "backend shard (8/16)",
                    "job_id": 105093815208,
                    "run_id": 35187743815,
                    "conclusion": "failure",
                    "run_status": "in_progress",
                }
            ],
            [],
        ),
    )
    assert ci_utils.main(["42", "--rerun-failed-jobs"]) == 5
    out = capsys.readouterr().out
    assert "Waiting: backend shard (8/16)" in out
    assert "in_progress" in out
    assert "Re-run this command once the run finishes." in out


def test_rerun_failed_jobs_reports_mixed_outcomes(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A rerun that partly lands and partly waits prints both lines and exits
    5: the waiting remainder governs the exit (task #3764)."""
    monkeypatch.setattr(
        owner_operations,
        "rerun_failed_jobs",
        lambda _pr, _repo: (
            [{"name": "lint", "job_id": 104, "run_id": 11, "conclusion": "FAILURE"}],
            [
                {
                    "name": "backend shard (8/16)",
                    "job_id": 105093815208,
                    "run_id": 35187743815,
                    "conclusion": "failure",
                    "run_status": "in_progress",
                }
            ],
            [],
        ),
    )
    assert ci_utils.main(["42", "--rerun-failed-jobs"]) == 5
    out = capsys.readouterr().out
    assert "Re-ran lint" in out
    assert "Waiting: backend shard (8/16)" in out


def test_rerun_failed_jobs_query_failure_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A failed GitHub query must not read as "no failed jobs" (issue #1945):
    the error is reported on stderr and the exit code flips to 1."""

    def _boom(_pr: Any, _repo: Any) -> list[dict]:
        raise owner_operations.CiJobRerunError("gh api runs failed: rate limited")

    monkeypatch.setattr(owner_operations, "list_failed_jobs", _boom)
    assert ci_utils.main(["42", "--rerun-failed-jobs", "--dry-run"]) == 1
    captured = capsys.readouterr()
    assert "Failed to list failed jobs" in captured.err
    assert "No failed jobs" not in captured.out


def test_rerun_failed_jobs_query_failure_non_dry_run_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The non-dry-run path reports the same query failure instead of exiting 0."""

    def _boom(_pr: Any, _repo: Any) -> list[dict]:
        raise owner_operations.CiJobRerunError("gh pr view failed: not found")

    monkeypatch.setattr(owner_operations, "rerun_failed_jobs", _boom)
    assert ci_utils.main(["42", "--rerun-failed-jobs"]) == 1
    assert "Failed to list failed jobs" in capsys.readouterr().err


def test_rerun_failed_jobs_exclusive_with_wait() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--rerun-failed-jobs", "--wait"])


def test_queue_status_prints_state_and_items(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _urlopen_sequence(
            [
                _TrunkResponse(
                    {
                        "state": "running",
                        "concurrency": 5,
                        "enqueuedPullRequests": [
                            {
                                "state": "testing",
                                "prNumber": 1900,
                                "prTitle": "feat: x",
                                "priorityName": "high",
                                "prSha": "abc123abc123abc123",
                            },
                            {
                                "state": "pending",
                                "prNumber": 1901,
                                "prTitle": "fix: y",
                                "priorityName": "medium",
                                "prSha": "def456def456def456",
                            },
                        ],
                    }
                )
            ],
            requests,
        ),
    )
    assert ci_utils.main(["--queue-status"]) == 0
    out = capsys.readouterr().out
    assert "state=running" in out
    assert "#1900 [testing] feat: x" in out
    assert "#1901 [pending] fix: y" in out
    assert requests[0].full_url == "https://api.trunk.io/v1/getQueue"
    assert requests[0].get_header("X-api-token") == "test-token"
    assert json.loads(cast(bytes, requests[0].data)) == {
        "repo": {"host": "github.com", "owner": "zhiyuan-zhang0206", "name": "Ava"},
        "targetBranch": "main",
    }


def test_queue_status_empty_queue(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _urlopen_sequence([_TrunkResponse({"state": "running", "enqueuedPullRequests": []})], []),
    )
    assert ci_utils.main(["--queue-status"]) == 0
    assert "No PRs in the queue" in capsys.readouterr().out


def test_queue_status_json_prints_raw_payload(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _urlopen_sequence([_TrunkResponse({"state": "running", "enqueuedPullRequests": []})], []),
    )
    assert ci_utils.main(["--queue-status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "running"


def test_queue_status_api_error_exits_three(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _urlopen_sequence([urllib.error.URLError("network down")], []),
    )
    assert ci_utils.main(["--queue-status"]) == 3
    assert "Trunk queue status error" in capsys.readouterr().err


def test_queue_status_requires_token(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TRUNK_API_TOKEN", raising=False)
    assert ci_utils.main(["--queue-status"]) == 3
    assert "TRUNK_API_TOKEN is required" in capsys.readouterr().err


def test_evict_success_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _urlopen_sequence([_TrunkResponse({})], requests),
    )
    assert ci_utils.main(["1877", "--evict"]) == 0
    assert "PR #1877 cancelled from the Trunk merge queue" in capsys.readouterr().err
    assert requests[0].full_url == "https://api.trunk.io/v1/cancelPullRequest"
    assert json.loads(cast(bytes, requests[0].data)) == {
        "repo": {"host": "github.com", "owner": "zhiyuan-zhang0206", "name": "Ava"},
        "pr": {"number": 1877},
        "targetBranch": "main",
    }


def test_evict_not_in_queue_exits_one(monkeypatch, capsys) -> None:
    """cancelPullRequest answers 404 when the PR is not in the queue; a
    nonzero exit keeps a typo'd PR number from reading as a clean evict."""
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    responses: list[_TrunkResponse | urllib.error.URLError] = []
    responses.append(
        urllib.error.HTTPError(
            "https://api.trunk.io/v1/cancelPullRequest",
            404,
            "Not Found",
            email.message.Message(),
            None,
        )
    )
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_sequence(responses, []))
    assert ci_utils.main(["1877", "--evict"]) == 1
    assert "not in the Trunk merge queue" in capsys.readouterr().err


def test_evict_api_error_exits_four(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        _urlopen_sequence([urllib.error.URLError("network down")], []),
    )
    assert ci_utils.main(["1877", "--evict"]) == 4
    assert "Trunk queue cancel error" in capsys.readouterr().err


def test_evict_requires_pr_number() -> None:
    with pytest.raises(SystemExit) as e:
        ci_utils.main(["--evict"])
    assert e.value.code == 2
