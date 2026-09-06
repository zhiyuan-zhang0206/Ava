"""Tests for scripts/ci_utils.py — the tool AGENTS.md gates merges on.

The verdict this file cares most about is the one that was wrong on 2026-07-28:
when Actions cannot schedule (#885 switched to hosted runners a private repo has
no minutes for), every workflow check vanishes from the rollup, the only check
left is a GitHub App's, it passes, and `check_ci` reported "CI all green (1
checks passed)" on a pull request that ran nothing.
"""

from __future__ import annotations

import email.message
import importlib.util
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ci_utils.py"
_MOD_NAME = "ci_utils_under_test"
_spec = importlib.util.spec_from_file_location(_MOD_NAME, _MOD_PATH)
assert _spec and _spec.loader
ci_utils = importlib.util.module_from_spec(_spec)
# Register before exec: @dataclass resolves its own module out of sys.modules
# while the class body is being processed, and fails on a module that is not
# there yet.
sys.modules[_MOD_NAME] = ci_utils
_spec.loader.exec_module(ci_utils)

CIStatus = ci_utils.CIStatus


def _check(
    name: str,
    conclusion: str,
    *,
    workflow: str = "CI",
    status: str = "COMPLETED",
    completed_at: str | None = None,
) -> dict:
    """One statusCheckRollup entry. `workflow=""` models a GitHub App check."""
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "workflowName": workflow,
        "completedAt": completed_at,
    }


_APP_CHECK = _check("GitGuardian Security Checks", "SUCCESS", workflow="")


class _TrunkResponse:
    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> _TrunkResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def _urlopen_sequence(
    responses: list[_TrunkResponse | urllib.error.URLError],
    seen_requests: list[urllib.request.Request],
):
    response_iter = iter(responses)

    def urlopen(request: urllib.request.Request, *, timeout: float) -> _TrunkResponse:
        seen_requests.append(request)
        response = next(response_iter)
        if isinstance(response, urllib.error.URLError):
            raise response
        return response

    return urlopen


def _labels_runner(labels: list[str], calls: list[list[str]]):
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"labels": [{"name": label} for label in labels]}),
            stderr="",
        )

    return run


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch):
    """Stub the `gh` subprocess, dispatching on the subcommand.

    `check_ci` makes two different calls — `gh pr view` for the rollup and `gh
    api .../actions/runs` for what Actions has scheduled — and they must not be
    answered with the same payload: telling "no workflow ever ran" from "the
    workflow has not attached a check yet" is exactly what the second call is
    for. `scheduled` is the names of runs the API reports as not yet completed.
    """

    def _install(
        checks: list[dict],
        mergeable: str = "MERGEABLE",
        *,
        scheduled: list[str] | None = None,
    ) -> None:
        rollup = json.dumps(
            {"mergeable": mergeable, "statusCheckRollup": checks, "headRefOid": "deadbeef"}
        )
        runs = json.dumps(scheduled or [])

        def _run(cmd, *_a, **_k):
            class _R:
                returncode = 0
                stdout = runs if "api" in cmd else rollup
                stderr = ""

            return _R()

        monkeypatch.setattr(ci_utils.subprocess, "run", _run)

    return _install


@pytest.fixture
def has_workflows(monkeypatch: pytest.MonkeyPatch):
    def _set(value: bool) -> None:
        monkeypatch.setattr(ci_utils, "_repo_has_workflows", lambda: value)

    return _set


# --- the regression this file exists for ---


def test_app_check_alone_is_not_green(gh: Any, has_workflows: Any) -> None:
    """The 2026-07-28 shape: Actions never ran, an app check passed."""
    gh([_APP_CHECK])
    has_workflows(True)
    r = ci_utils.check_ci("886")
    assert r.verdict is CIStatus.NO_WORKFLOW_RUNS
    assert r.workflow_checks == []
    assert "DID NOT RUN" in r.summary()


def test_app_check_alone_is_green_when_repo_has_no_workflows(gh: Any, has_workflows: Any) -> None:
    """A repo with no workflow files is legitimately green on app checks alone."""
    gh([_APP_CHECK])
    has_workflows(False)
    assert ci_utils.check_ci("1").verdict is CIStatus.ALL_PASSED


def test_one_workflow_check_is_enough_to_be_green(gh: Any, has_workflows: Any) -> None:
    """The guard asks whether the suite ran at all — not how many jobs it has."""
    gh([_APP_CHECK, _check("backend (pytest + pyright)", "SUCCESS")])
    has_workflows(True)
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.ALL_PASSED
    assert r.workflow_checks == ["backend (pytest + pyright)"]


# --- the verdicts that must not regress ---


def test_full_green_suite(gh: Any, has_workflows: Any) -> None:
    gh(
        [
            _check("changes (path filter)", "SUCCESS"),
            _check("backend (pytest + pyright)", "SUCCESS"),
            _check("docs-only (pass-through)", "SKIPPED"),
            _APP_CHECK,
        ]
    )
    has_workflows(True)
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.ALL_PASSED
    assert len(r.passed) == 4


def test_failure_wins_over_the_workflow_guard(gh: Any, has_workflows: Any) -> None:
    """A real failure must report FAILED, never the did-not-run verdict."""
    gh([_APP_CHECK, _check("backend (pytest + pyright)", "FAILURE")])
    has_workflows(True)
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.FAILED
    assert r.failed == [{"name": "backend (pytest + pyright)", "conclusion": "FAILURE"}]


def test_pending_wins_over_the_workflow_guard(gh: Any, has_workflows: Any) -> None:
    """Still-running checks mean wait, not did-not-run."""
    gh([_APP_CHECK, _check("backend", "", status="IN_PROGRESS")])
    has_workflows(True)
    assert ci_utils.check_ci("1").verdict is CIStatus.PENDING


def test_unknown_conclusion_counts_as_pending(gh: Any, has_workflows: Any) -> None:
    """A COMPLETED check with an unrecognized conclusion must not read as green."""
    gh([_check("weird", "SOMETHING_NEW")])
    has_workflows(True)
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.PENDING
    assert r.pending == ["weird"]


def test_merge_conflict_short_circuits(gh: Any, has_workflows: Any) -> None:
    gh([_check("backend", "SUCCESS")], mergeable="CONFLICTING")
    has_workflows(True)
    assert ci_utils.check_ci("1").verdict is CIStatus.MERGE_CONFLICT


def test_merge_conflict_keeps_qa_approved_gate_out_of_completed(
    gh: Any, has_workflows: Any
) -> None:
    gate = _check("qa-approved-gate", "FAILURE", workflow="QA approved gate")
    evidence = _check("evaluate-qa-evidence", "FAILURE", workflow="QA approved gate")
    gh([_check("backend", "SUCCESS"), gate, evidence], mergeable="CONFLICTING")
    has_workflows(True)
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.MERGE_CONFLICT
    assert r.completed == ["backend"]
    assert r.gate_checks == [gate, evidence]


def test_status_context_gate_success_is_not_a_pending_check(gh: Any, has_workflows: Any) -> None:
    """qa_gate.py publishes the receipt as a commit STATUS (StatusContext):
    it carries context+state, no name/status/conclusion. It must land in
    gate_checks, never read as a nameless "?" pending entry (2026-09-04:
    five green PRs froze on exactly that phantom)."""
    status_ctx = {
        "__typename": "StatusContext",
        "context": "qa-approved-gate",
        "state": "SUCCESS",
        "targetUrl": "",
    }
    gh([_check("backend (pytest + pyright)", "SUCCESS"), status_ctx])
    has_workflows(True)
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.ALL_PASSED
    assert r.gate_checks == [status_ctx]
    assert "?" not in r.pending


def test_status_context_other_context_success_counts_as_passed(gh: Any, has_workflows: Any) -> None:
    status_ctx = {
        "__typename": "StatusContext",
        "context": "coverage/deploy",
        "state": "SUCCESS",
        "targetUrl": "",
    }
    gh([_check("backend (pytest + pyright)", "SUCCESS"), status_ctx])
    has_workflows(True)
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.ALL_PASSED
    assert "coverage/deploy" in r.passed


def test_status_context_failure_counts_as_failed(gh: Any, has_workflows: Any) -> None:
    status_ctx = {
        "__typename": "StatusContext",
        "context": "coverage/deploy",
        "state": "FAILURE",
        "targetUrl": "",
    }
    gh([_check("backend (pytest + pyright)", "SUCCESS"), status_ctx])
    has_workflows(True)
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.FAILED
    assert {"name": "coverage/deploy", "conclusion": "FAILURE"} in r.failed


def test_status_context_pending_state_is_pending(gh: Any, has_workflows: Any) -> None:
    status_ctx = {
        "__typename": "StatusContext",
        "context": "coverage/deploy",
        "state": "PENDING",
        "targetUrl": "",
    }
    gh([_check("backend (pytest + pyright)", "SUCCESS"), status_ctx])
    has_workflows(True)
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.PENDING
    assert r.pending == ["coverage/deploy"]


def test_stale_cancelled_run_loses_to_newer_success_of_same_name(
    gh: Any, has_workflows: Any
) -> None:
    """cancel-in-progress on the QA evaluator leaves a CANCELLED run and a
    SUCCESS run of the same name on one SHA; GitHub treats them as one
    logical check whose state is the newest run's. The survivor is QA evidence,
    not a verdict check (2026-09-04 #1636)."""
    stale = _check(
        "evaluate-qa-evidence",
        "CANCELLED",
        workflow="QA Approved Gate",
        completed_at="2026-09-03T17:25:42Z",
    )
    fresh = _check(
        "evaluate-qa-evidence",
        "SUCCESS",
        workflow="QA Approved Gate",
        completed_at="2026-09-03T18:04:17Z",
    )
    gh(
        [
            _check("backend (pytest + pyright)", "SUCCESS"),
            stale,
            fresh,
        ]
    )
    has_workflows(True)
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.ALL_PASSED
    assert r.gate_checks == [fresh]
    assert "evaluate-qa-evidence" not in r.completed


def test_cancelled_qa_evidence_run_stays_out_of_ci_verdict(gh: Any, has_workflows: Any) -> None:
    """The queue's own gate evaluates QA evidence; the CI verdict must not block on it."""
    cancelled = _check(
        "evaluate-qa-evidence",
        "CANCELLED",
        workflow="QA Approved Gate",
        completed_at="2026-09-03T17:25:42Z",
    )
    gh(
        [
            _check("backend (pytest + pyright)", "SUCCESS"),
            cancelled,
        ]
    )
    has_workflows(True)
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.ALL_PASSED
    assert r.failed == []
    assert r.gate_checks == [cancelled]


def test_empty_rollup(gh: Any, has_workflows: Any) -> None:
    gh([])
    has_workflows(True)
    assert ci_utils.check_ci("1").verdict is CIStatus.NO_CHECKS


def test_gh_failure_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _R:
        returncode = 1
        stdout = ""
        stderr = "gh: not authenticated"

    monkeypatch.setattr(ci_utils.subprocess, "run", lambda *_a, **_k: _R())
    r = ci_utils.check_ci("1")
    assert r.verdict is CIStatus.ERROR
    assert "not authenticated" in r.error_detail


# --- the real repo ---


def test_this_repo_has_workflows() -> None:
    """The guard is only armed where workflows exist; here they do."""
    assert ci_utils._repo_has_workflows() is True


# --- the false negative: scheduled but not yet reporting ---
# The mirror of the regression above. Between a push and the first check-run
# appearing on the commit, the rollup looks exactly like "Actions never ran" —
# and that window lands on the first poll after a push, which is when an agent
# is most likely to be watching. Reporting DID NOT RUN there sends it off to
# investigate a CI that is simply still starting.


def test_queued_run_with_no_check_yet_is_pending_not_did_not_run(
    gh: Any, has_workflows: Any
) -> None:
    gh([_APP_CHECK], scheduled=["CI"])
    has_workflows(True)

    r = ci_utils.check_ci("901")

    assert r.verdict == CIStatus.PENDING
    assert "CI" in r.pending
    assert "DID NOT RUN" not in r.summary()


def test_nothing_scheduled_is_still_did_not_run(gh: Any, has_workflows: Any) -> None:
    """The guard must keep working: an app check alone, and nothing queued to
    explain it, is the shape that shipped a PR having run no tests."""
    gh([_APP_CHECK], scheduled=[])
    has_workflows(True)

    r = ci_utils.check_ci("886")

    assert r.verdict == CIStatus.NO_WORKFLOW_RUNS


def test_runs_api_failure_keeps_the_conservative_verdict(
    gh: Any, has_workflows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that cannot answer must not invent a reason to wait — an
    unreachable API is not evidence that CI is coming."""
    gh([_APP_CHECK])
    has_workflows(True)
    monkeypatch.setattr(ci_utils, "_runs_not_yet_reporting", lambda *_a, **_k: [])

    assert ci_utils.check_ci("886").verdict == CIStatus.NO_WORKFLOW_RUNS


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

    monkeypatch.setattr(ci_utils.subprocess, "run", _run)

    assert ci_utils._runs_not_yet_reporting("abc123", None) == ["CI"]
    joined = " ".join(seen["cmd"])
    assert "head_sha=abc123" in joined
    assert 'select(.status != "completed")' in joined


def test_runs_probe_returns_empty_on_gh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(ci_utils.subprocess, "run", lambda *_a, **_k: _R())

    assert ci_utils._runs_not_yet_reporting("abc123", None) == []


def test_runs_probe_survives_unparseable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class _R:
        returncode = 0
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(ci_utils.subprocess, "run", lambda *_a, **_k: _R())

    assert ci_utils._runs_not_yet_reporting("abc123", None) == []


_QA_APPROVED_GATE_FAILURE = _check("qa-approved-gate", "FAILURE", workflow="QA approved gate")


def test_qa_approved_gate_failure_is_excluded_from_ci_verdict(gh: Any, has_workflows: Any) -> None:
    """The QA label gate is enforced by the queue, not the CI verdict."""
    gh([_check("backend (pytest + pyright)", "SUCCESS"), _QA_APPROVED_GATE_FAILURE])
    has_workflows(True)
    r = ci_utils.check_ci("57")
    assert r.verdict is CIStatus.ALL_PASSED
    assert "qa-approved-gate" not in r.failed
    assert "qa-approved-gate" not in r.workflow_checks
    assert r.gate_checks == [_QA_APPROVED_GATE_FAILURE]


def test_qa_evidence_failure_is_excluded_from_ci_verdict(gh: Any, has_workflows: Any) -> None:
    """The QA evidence evaluator is enforced by the queue, not the CI verdict."""
    evidence = _check("evaluate-qa-evidence", "FAILURE", workflow="QA approved gate")
    gh([_check("backend (pytest + pyright)", "SUCCESS"), evidence])
    has_workflows(True)
    r = ci_utils.check_ci("57")
    assert r.verdict is CIStatus.ALL_PASSED
    assert "evaluate-qa-evidence" not in r.failed
    assert "evaluate-qa-evidence" not in r.workflow_checks
    assert r.gate_checks == [evidence]


def _completed(stdout: str = "", returncode: int = 0) -> Any:
    return ci_utils.subprocess.CompletedProcess([], returncode, stdout, "boom")


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
    assert ci_utils._parse_ts(value) == expected


@pytest.mark.parametrize(
    ("last", "now", "expected"), [(90.0, 100.0, 290), (0.0, 400.0, 0), (None, 100.0, 0)]
)
def test_queue_cooldown_seconds(
    monkeypatch: pytest.MonkeyPatch, last: float | None, now: float, expected: int
) -> None:
    monkeypatch.setattr(ci_utils, "_last_head_update", lambda *_a: last)
    monkeypatch.setattr(ci_utils.time, "time", lambda: now)
    assert ci_utils._queue_cooldown_seconds("7", "o/r") == expected


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
        ci_utils.subprocess, "run", lambda cmd, **_k: seen.append(cmd) or next(reply_iter)
    )
    assert ci_utils._last_head_update("7", "o/r") == expected
    assert "/issues/7/timeline" in " ".join(seen[0])
    assert "/pulls/7/commits" in " ".join(seen[1])


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_utils.time, "sleep", lambda _s: None)


@pytest.fixture
def poll(monkeypatch: pytest.MonkeyPatch):
    """Stub check_ci with a queue of verdicts; main() consumes them in order,
    the last one repeating forever (a settled verdict ends the loop)."""

    def _install(*verdicts: CIStatus) -> None:
        calls = {"n": 0}

        def fake_check(pr, *, repo):
            i = min(calls["n"], len(verdicts) - 1)
            calls["n"] += 1
            v = verdicts[i]
            if v is CIStatus.ALL_PASSED:
                return ci_utils.CIResult(verdict=v, passed=["lint", "test"])
            if v is CIStatus.FAILED:
                return ci_utils.CIResult(
                    verdict=v, failed=[{"name": "lint", "conclusion": "FAILURE"}]
                )
            if v is CIStatus.ERROR:
                return ci_utils.CIResult(verdict=v, error_detail="gh CLI error: boom")
            return ci_utils.CIResult(verdict=v)

        monkeypatch.setattr(ci_utils, "check_ci", fake_check)

    return _install


def test_wait_all_passed_exits_zero(no_sleep, poll, capsys) -> None:
    poll(CIStatus.ALL_PASSED)
    assert ci_utils.main(["1243", "--wait"]) == 0
    assert "CI green" in capsys.readouterr().out


@pytest.mark.parametrize(
    "verdict",
    [CIStatus.FAILED, CIStatus.MERGE_CONFLICT, CIStatus.NO_WORKFLOW_RUNS],
)
def test_wait_not_green_exits_one(no_sleep, poll, capsys, verdict) -> None:
    poll(verdict)
    assert ci_utils.main(["1243", "--wait"]) == 1
    assert "NOT green" in capsys.readouterr().err


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
    monkeypatch.setattr(ci_utils.time, "sleep", lambda _s: None)
    assert ci_utils.main(["1243", "--wait", "--timeout", "1"]) == 1
    assert "timed out" in capsys.readouterr().err


def test_wait_merge_trunk_submits_and_lands_when_green(no_sleep, poll, monkeypatch, capsys) -> None:
    poll(CIStatus.ALL_PASSED)
    label_calls: list[list[str]] = []
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(ci_utils, "_queue_cooldown_seconds", lambda *_a, **_k: 0)
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(ci_utils.subprocess, "run", _labels_runner(["qa-approved"], label_calls))
    monkeypatch.setattr(
        ci_utils.urllib.request,
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
    # (qa-approved label verified first), then wait for the queue to land it.
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
    assert label_calls[0][:4] == ["gh", "pr", "view", "1243"]


def test_wait_merge_trunk_failed_state_prints_full_payload(
    no_sleep, poll, monkeypatch, capsys
) -> None:
    """Task #2541: a failed/cancelled Trunk submission often carries an empty
    `reason`, so the old one-line print was undiagnosable (five first-submit
    failures 2026-09-06 left no trace). The full getSubmittedPullRequest
    payload must be printed on the terminal branch."""
    poll(CIStatus.ALL_PASSED)
    monkeypatch.setattr(ci_utils, "_queue_cooldown_seconds", lambda *_a, **_k: 0)
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(ci_utils.subprocess, "run", _labels_runner(["qa-approved"], []))
    monkeypatch.setattr(
        ci_utils.urllib.request,
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
    monkeypatch.setattr(ci_utils, "_queue_cooldown_seconds", lambda *_a, **_k: 0)
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(ci_utils.subprocess, "run", _labels_runner(["qa-approved"], []))
    monkeypatch.setattr(
        ci_utils.urllib.request,
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


# --- is_terminal: watchers must never guess verdict strings -------------------
# 2026-08-03: a watcher hard-coded ("success", "failure", "merged") — none of
# which exist — and spun silently forever while CI went green. `is_terminal`
# gives watchers a predicate instead of a string to mistype.


@pytest.mark.parametrize(
    "verdict",
    [
        CIStatus.ALL_PASSED,
        CIStatus.FAILED,
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
    assert payload["gate_checks"] == []


def test_json_probe_pending_is_not_terminal(gh: Any, has_workflows: Any, capsys) -> None:
    gh([_APP_CHECK, _check("backend", "", status="IN_PROGRESS")])
    has_workflows(True)
    assert ci_utils.main(["1243", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "pending"
    assert payload["terminal"] is False


# --- --rerun-failed-jobs CLI (issue #102) ---


def test_rerun_failed_jobs_dry_run_lists_jobs(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """--dry-run lists the failed jobs and exits 0 without re-running anything."""
    monkeypatch.setattr(
        ci_utils,
        "list_failed_jobs",
        lambda _pr, _repo: [
            {"name": "e2e shard (3/4)", "job_id": 102, "run_id": 11, "conclusion": "FAILURE"}
        ],
    )
    assert ci_utils.main(["42", "--rerun-failed-jobs", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "e2e shard (3/4)" in out and "102" in out


def test_rerun_failed_jobs_nothing_to_do(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(ci_utils, "list_failed_jobs", lambda _pr, _repo: [])
    assert ci_utils.main(["42", "--rerun-failed-jobs", "--dry-run"]) == 0
    assert "No failed jobs" in capsys.readouterr().out


def test_rerun_failed_jobs_forwards_and_reports_errors(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """A rejected rerun request is printed and flips the exit code to 3."""
    monkeypatch.setattr(
        ci_utils,
        "rerun_failed_jobs",
        lambda _pr, _repo: ([], ["lint: gh: rate limited"]),
    )
    assert ci_utils.main(["42", "--rerun-failed-jobs"]) == 3
    assert "rate limited" in capsys.readouterr().out


def test_rerun_failed_jobs_success_exits_zero(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        ci_utils,
        "rerun_failed_jobs",
        lambda _pr, _repo: (
            [{"name": "lint", "job_id": 104, "run_id": 11, "conclusion": "FAILURE"}],
            [],
        ),
    )
    assert ci_utils.main(["42", "--rerun-failed-jobs"]) == 0
    assert "Re-ran lint" in capsys.readouterr().out


def test_rerun_failed_jobs_exclusive_with_wait() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--rerun-failed-jobs", "--wait"])


# --- Trunk queue operator commands: --queue-status / --evict ---


def test_queue_status_prints_state_and_items(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    requests: list[urllib.request.Request] = []
    monkeypatch.setattr(
        ci_utils.urllib.request,
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
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence([_TrunkResponse({"state": "running", "enqueuedPullRequests": []})], []),
    )
    assert ci_utils.main(["--queue-status"]) == 0
    assert "No PRs in the queue" in capsys.readouterr().out


def test_queue_status_json_prints_raw_payload(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence([_TrunkResponse({"state": "running", "enqueuedPullRequests": []})], []),
    )
    assert ci_utils.main(["--queue-status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "running"


def test_queue_status_api_error_exits_three(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(
        ci_utils.urllib.request,
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
        ci_utils.urllib.request,
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
    monkeypatch.setattr(ci_utils.urllib.request, "urlopen", _urlopen_sequence(responses, []))
    assert ci_utils.main(["1877", "--evict"]) == 1
    assert "not in the Trunk merge queue" in capsys.readouterr().err


def test_evict_api_error_exits_four(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence([urllib.error.URLError("network down")], []),
    )
    assert ci_utils.main(["1877", "--evict"]) == 4
    assert "Trunk queue cancel error" in capsys.readouterr().err


def test_evict_requires_pr_number() -> None:
    with pytest.raises(SystemExit) as e:
        ci_utils.main(["--evict"])
    assert e.value.code == 2


def test_queue_ops_exclusive_with_merge_wait() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(["--queue-status", "--merge"])
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--evict", "--wait"])
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--evict", "--queue-status"])
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--evict", "--json"])


# --- --diagnose: Trunk/CI failure triage (task #2572) ---


@pytest.fixture
def diag_gh(monkeypatch):
    """Stub the gh/git subprocess for --diagnose: canned responses keyed by a
    substring of the command line, consumed in call order (first entry whose
    key matches the current call)."""

    def _install(responses: list[tuple[str, str]]) -> None:
        def run(cmd, *_a, **_k):
            joined = " ".join(str(c) for c in cmd)
            for idx, (key, out) in enumerate(responses):
                if key in joined:
                    responses.pop(idx)
                    return subprocess.CompletedProcess(cmd, 0, out, "")
            return subprocess.CompletedProcess(cmd, 1, "", "unmatched: " + joined)

        monkeypatch.setattr(ci_utils.subprocess, "run", run)

    return _install


def _diag_pr_view(mergeable: str = "MERGEABLE", checks: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "mergeable": mergeable,
            "labels": [{"name": "qa-approved"}],
            "headRefOid": "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111",
            "state": "OPEN",
            "statusCheckRollup": checks or [],
        }
    )


def _diag_check(name: str, conclusion: str = "FAILURE") -> dict:
    return {"name": name, "conclusion": conclusion, "status": "COMPLETED"}


def _diag_job(name: str, job_id: int = 9, conclusion: str = "FAILURE") -> dict:
    return {"name": name, "job_id": job_id, "run_id": 10, "conclusion": conclusion}


def test_diagnose_merge_conflict(diag_gh, monkeypatch, capsys) -> None:
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(mergeable="CONFLICTING")),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", "[]"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    out = capsys.readouterr().out
    assert "merge_conflict" in out
    assert "rebase on origin/main" in out


def test_diagnose_lint_hard_limit(diag_gh, monkeypatch, capsys) -> None:
    check = "backend structure (pre-commit lint + codegen freshness)"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '["10"]'),
            ("/jobs", json.dumps([_diag_job(check)])),
            ("/logs", "scripts/host.py file is 812 lines, over the 800-line hard ceiling"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    out = capsys.readouterr().out
    assert "lint hard limit" in out
    assert "split the file" in out


def test_diagnose_first_load_budget(diag_gh, monkeypatch, capsys) -> None:
    check = "Production build + first-load JavaScript budget"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '["10"]'),
            ("/jobs", json.dumps([_diag_job(check)])),
            ("/logs", "First Load JS shared by all is 512 kB (budget 500 kB)"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "first-load JavaScript budget" in capsys.readouterr().out


def test_diagnose_visual_regression(diag_gh, monkeypatch, capsys) -> None:
    check = "e2e shard (3/4)"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '["10"]'),
            ("/jobs", json.dumps([_diag_job(check)])),
            ("/logs", "toMatchImageSnapshot failed: baseline image differs"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "visual regression" in capsys.readouterr().out


def test_diagnose_truncate_lint_delta_case(diag_gh, monkeypatch, capsys) -> None:
    """The #1871 delta case: truncate-isolation lint's comment-stripping
    regex falsely captured the word delta — a deterministic lint failure."""
    check = "lint truncate isolation"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '["10"]'),
            ("/jobs", json.dumps([_diag_job(check)])),
            (
                "/logs",
                "truncate-isolation lint: comment-stripping regex captured CREATE TABLE delta",
            ),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "truncate-isolation lint" in capsys.readouterr().out


def test_diagnose_clock_lattice_gateway_case(diag_gh, monkeypatch, capsys) -> None:
    """The #1871 gateway closure case: a constant outside its family module."""
    check = "lint clock lattice"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '["10"]'),
            ("/jobs", json.dumps([_diag_job(check)])),
            ("/logs", "lattice-vocabulary clock constant defined outside its family module"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "clock-lattice lint" in capsys.readouterr().out


def test_diagnose_known_flake_matches_quarantined(diag_gh, monkeypatch, capsys) -> None:
    """The #1871 consumer_guard case: failing test is in Trunk's flaky DB."""
    check = "backend shard (4/16)"
    monkeypatch.setenv("TRUNK_API_TOKEN", "test-token")
    monkeypatch.setattr(
        ci_utils.urllib.request,
        "urlopen",
        _urlopen_sequence(
            [
                _TrunkResponse(
                    {
                        "quarantined_tests": [
                            {
                                "name": "test_consumer_guard_queue_backpressure",
                                "file": "tests/agent/test_consumer_guard.py",
                                "status": "FLAKY",
                            }
                        ]
                    }
                ),
                _TrunkResponse({"state": "testing"}),
            ],
            [],
        ),
    )
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '["10"]'),
            ("/jobs", json.dumps([_diag_job(check)])),
            (
                "/logs",
                "FAILED tests/agent/test_consumer_guard.py::test_consumer_guard_queue_backpressure",
            ),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    out = capsys.readouterr().out
    assert "known flake" in out
    assert "state=testing" in out


def test_diagnose_runner_network_flake(diag_gh, monkeypatch, capsys) -> None:
    check = "backend (pytest + pyright)"
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check(check)])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", '["10"]'),
            ("/jobs", json.dumps([_diag_job(check)])),
            ("/logs", "apt-get install failed: archive cache is empty — no offline fallback"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "runner-side network flake" in capsys.readouterr().out


def test_diagnose_reports_synthetic_test_pr(diag_gh, monkeypatch, capsys) -> None:
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view()),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", "[]"),
            (
                "state=all",
                json.dumps(
                    [
                        {
                            "number": 1875,
                            "state": "closed",
                            "head": {
                                "ref": "trunk-merge/pr-1871/e65e4a02-9624-435e-853a-8fb467af307c"
                            },
                        }
                    ]
                ),
            ),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    out = capsys.readouterr().out
    assert "synthetic test PR #1875" in out
    assert "trunk-merge/pr-1871/" in out


def test_diagnose_stale_base(diag_gh, monkeypatch, capsys) -> None:
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view()),
            ("--json baseRefOid --jq", "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", "[]"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    assert "stale_base" in capsys.readouterr().out


def test_diagnose_json_machine_readable(diag_gh, monkeypatch, capsys) -> None:
    diag_gh(
        [
            ("--json mergeable", _diag_pr_view(checks=[_diag_check("lint clock lattice")])),
            ("--json baseRefOid --jq", "cccc3333cccc3333cccc3333cccc3333cccc3333"),
            ("ls-remote", "cccc3333cccc3333cccc3333cccc3333cccc3333\trefs/heads/main"),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", "[]"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pr"] == "1871"
    assert payload["checks"][0]["classification"] == "deterministic lint failure"
    assert "issues" in payload and "synthetic_test_prs" in payload


def test_diagnose_exclusive_and_requires_pr() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--diagnose", "--wait"])
    with pytest.raises(SystemExit):
        ci_utils.main(["--diagnose"])


def test_diagnose_merged_pr_reports_no_conflict_or_stale_base(diag_gh, monkeypatch, capsys) -> None:
    """A merged PR has mergeable=UNKNOWN and a base behind main by definition —
    neither may be reported as a diagnosable problem."""
    view = json.loads(_diag_pr_view(mergeable="UNKNOWN"))
    view["state"] = "MERGED"
    diag_gh(
        [
            ("--json mergeable", json.dumps(view)),
            ("json headRefOid --jq", "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"),
            ("head_sha=", "[]"),
            ("state=all", "[]"),
        ]
    )
    assert ci_utils.main(["1871", "--diagnose"]) == 0
    out = capsys.readouterr().out
    assert "merge_conflict" not in out
    assert "stale_base" not in out


# --- --ci-usage: per-agent CI minute rollup (task #2575) ---


def test_ci_usage_prints_per_agent_rollup(monkeypatch, tmp_path, capsys) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "run_id": 1,
                "day": "2026-09-06",
                "agent_id": 5811,
                "linux_minutes": 100,
                "macos_minutes": 10,
            }
        )
        + "\n"
    )
    monkeypatch.setattr(ci_utils, "DEFAULT_LEDGER", ledger)
    assert ci_utils.main(["--ci-usage"]) == 0
    out = capsys.readouterr().out
    assert "#5811: 1 runs" in out
    assert "est $1.4" in out


def test_ci_usage_json_machine_readable(monkeypatch, tmp_path, capsys) -> None:
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(
            {
                "run_id": 1,
                "day": "2026-09-06",
                "agent_id": 5811,
                "linux_minutes": 10,
                "macos_minutes": 1,
            }
        )
        + "\n"
    )
    monkeypatch.setattr(ci_utils, "DEFAULT_LEDGER", ledger)
    assert ci_utils.main(["--ci-usage", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["agent_id"] == 5811
    assert rows[0]["linux_minutes"] == 10


def test_ci_usage_exclusive_and_no_pr() -> None:
    with pytest.raises(SystemExit):
        ci_utils.main(["42", "--ci-usage"])
    with pytest.raises(SystemExit):
        ci_utils.main(["--ci-usage", "--wait"])
