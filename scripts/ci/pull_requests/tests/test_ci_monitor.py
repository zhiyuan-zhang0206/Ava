"""Tests for the reusable CI verdicts and optional owner CLI operations.

The verdict this file cares most about was wrong on 2026-07-28: when Actions cannot schedule (#885
switched to hosted runners a private repo has no minutes for), every workflow check vanishes from
the rollup. The only check left is a GitHub App's; it passes, and `check_ci` reported "CI all green
(1 checks passed)" on a pull request that ran nothing.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.host.net.ports import UrlOpener
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import fake_host
from scripts.ci.pull_requests import status

# Untyped fixtures and helper calls throughout: the call-site rules stay at warning for this file.
# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning

CIStatus = status.CIStatus
TEST_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def make_clock(*, wall_start: datetime = TEST_NOW) -> Clock:
    timers = ManualTimers(wall_start=wall_start)
    return Clock(
        ClockConfig(
            timezone="UTC",
            authoritative_timezone="UTC",
            message_timestamp_weekday=False,
        ),
        timers=timers,
        now=timers.now,
    )


TEST_CLOCK = make_clock()


def status_check_rollup_entry(
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


APP_CHECK = status_check_rollup_entry("GitGuardian Security Checks", "SUCCESS", workflow="")


class TrunkResponse:
    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def __enter__(self) -> TrunkResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def urlopen_sequence(
    responses: list[TrunkResponse | urllib.error.URLError],
    seen_requests: list[urllib.request.Request],
) -> UrlOpener:
    response_iter = iter(responses)

    def urlopen(request: str | urllib.request.Request, timeout: float = 30.0) -> TrunkResponse:
        del timeout
        assert isinstance(request, urllib.request.Request)
        seen_requests.append(request)
        response = next(response_iter)
        if isinstance(response, urllib.error.URLError):
            raise response
        return response

    return urlopen


def gh_runner(calls: list[list[str]]) -> Host:
    def run(argv: Sequence[str], **_: object) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    return fake_host(run)


@pytest.fixture
def gh() -> Any:
    """Stub the `gh` subprocess, dispatching on the subcommand.

    `check_ci` makes three calls: `gh pr view` for the rollup, `gh api .../actions/runs` for what
    Actions scheduled, and the completed main-workflow probe. They need different payloads. The
    scheduled-runs call distinguishes "no workflow ever ran" from "the workflow has not attached a
    check yet". `scheduled` names runs not yet completed; `main_completed` defaults to True.
    """

    def _install(
        checks: list[dict],
        mergeable: str = "MERGEABLE",
        *,
        scheduled: list[str] | None = None,
        main_completed: bool = True,
        is_draft: bool = False,
    ) -> Host:
        rollup = json.dumps(
            {
                "mergeable": mergeable,
                "statusCheckRollup": checks,
                "headRefOid": "deadbeef",
                "isDraft": is_draft,
            }
        )
        runs = json.dumps(scheduled or [])

        def _run(
            argv: Sequence[str], *_a: object, **_k: object
        ) -> subprocess.CompletedProcess[str]:
            argv = list(argv)
            url = argv[2] if len(argv) > 2 else ""
            if "api" not in argv:
                stdout = rollup
            elif "status=completed" in url:
                # The main-workflow-visibility probe: `length` of the completed
                # main-workflow runs for this head.
                stdout = "1" if main_completed else "0"
            elif "actions/runs?" in url and "--jq" not in argv:
                # job_rerun reads the raw REST runs payload (issue #1945);
                # keep its view empty so diagnose tests stay deterministic.
                stdout = json.dumps({"total_count": 0, "workflow_runs": []})
            elif "/jobs?" in url:
                stdout = json.dumps({"total_count": 0, "jobs": []})
            else:
                stdout = runs
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        return fake_host(_run)

    return _install


@pytest.fixture
def has_workflows(monkeypatch: pytest.MonkeyPatch):
    def _set(value: bool) -> None:
        monkeypatch.setattr(status, "repo_has_workflows", lambda: value)

    return _set


# --- the regression this file exists for ---


def test_app_check_alone_is_not_green(gh: Any, has_workflows: Any) -> None:
    """The 2026-07-28 shape: Actions never ran, an app check passed."""
    host = gh([APP_CHECK], scheduled=[])
    has_workflows(True)
    r = status.check_ci("886", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.NO_WORKFLOW_RUNS
    assert r.workflow_checks == []
    assert "DID NOT RUN" in r.summary()


def test_app_check_alone_is_green_when_repo_has_no_workflows(gh: Any, has_workflows: Any) -> None:
    """A repo with no workflow files is legitimately green on app checks alone."""
    host = gh([APP_CHECK])
    has_workflows(False)
    assert status.check_ci("1", host=host, clock=TEST_CLOCK).verdict is CIStatus.ALL_PASSED


def test_one_workflow_check_is_enough_to_be_green(gh: Any, has_workflows: Any) -> None:
    """The guard asks whether the suite ran at all — not how many jobs it has."""
    host = gh([APP_CHECK, status_check_rollup_entry("backend (pytest + pyright)", "SUCCESS")])
    has_workflows(True)
    r = status.check_ci("1", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.ALL_PASSED
    assert r.workflow_checks == ["backend (pytest + pyright)"]


# --- the verdicts that must not regress ---


def test_full_green_suite(gh: Any, has_workflows: Any) -> None:
    host = gh(
        [
            status_check_rollup_entry("changes (path filter)", "SUCCESS"),
            status_check_rollup_entry("backend (pytest + pyright)", "SUCCESS"),
            status_check_rollup_entry("docs-only (pass-through)", "SKIPPED"),
            APP_CHECK,
        ]
    )
    has_workflows(True)
    r = status.check_ci("1", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.ALL_PASSED
    assert len(r.passed) == 4


def test_failure_wins_over_the_workflow_guard(gh: Any, has_workflows: Any) -> None:
    """A real failure must report FAILED, never the did-not-run verdict."""
    host = gh([APP_CHECK, status_check_rollup_entry("backend (pytest + pyright)", "FAILURE")])
    has_workflows(True)
    r = status.check_ci("1", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.FAILED
    assert r.failed == [{"name": "backend (pytest + pyright)", "conclusion": "FAILURE"}]


def test_pending_wins_over_the_workflow_guard(gh: Any, has_workflows: Any) -> None:
    """Still-running checks mean wait, not did-not-run."""
    host = gh([APP_CHECK, status_check_rollup_entry("backend", "", status="IN_PROGRESS")])
    has_workflows(True)
    assert status.check_ci("1", host=host, clock=TEST_CLOCK).verdict is CIStatus.PENDING


def test_unknown_conclusion_counts_as_pending(gh: Any, has_workflows: Any) -> None:
    """A COMPLETED check with an unrecognized conclusion must not read as green."""
    host = gh([status_check_rollup_entry("weird", "SOMETHING_NEW")])
    has_workflows(True)
    r = status.check_ci("1", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.PENDING
    assert r.pending == ["weird"]


def test_merge_conflict_short_circuits(gh: Any, has_workflows: Any) -> None:
    host = gh([status_check_rollup_entry("backend", "SUCCESS")], mergeable="CONFLICTING")
    has_workflows(True)
    assert status.check_ci("1", host=host, clock=TEST_CLOCK).verdict is CIStatus.MERGE_CONFLICT


@pytest.mark.parametrize(
    ("context", "state", "verdict", "bucket", "expected"),
    [
        ("coverage/deploy", "SUCCESS", CIStatus.ALL_PASSED, "passed", "coverage/deploy"),
        ("coverage/deploy", "FAILURE", CIStatus.FAILED, "failed", "failure"),
        ("coverage/deploy", "PENDING", CIStatus.PENDING, "pending", "coverage/deploy"),
    ],
)
def test_status_context_bucketing(
    gh: Any,
    has_workflows: Any,
    context: str,
    state: str,
    verdict: CIStatus,
    bucket: str,
    expected: str,
) -> None:
    """Commit statuses have context+state, not check-run keys; one must not
    become a phantom '?' pending check (2026-09-04: five PRs froze)."""
    status_ctx = {
        "__typename": "StatusContext",
        "context": context,
        "state": state,
        "targetUrl": "",
    }
    host = gh([status_check_rollup_entry("backend (pytest + pyright)", "SUCCESS"), status_ctx])
    has_workflows(True)
    result = status.check_ci("1", host=host, clock=TEST_CLOCK)
    assert result.verdict is verdict
    assert "?" not in result.pending
    if bucket == "failed":
        assert {"name": context, "conclusion": state} in result.failed
    elif bucket == "pending":
        assert result.pending == [context]
    else:
        assert expected in result.passed


def test_stale_cancelled_run_loses_to_newer_success_of_same_name(
    gh: Any, has_workflows: Any
) -> None:
    """cancel-in-progress leaves a CANCELLED run and a SUCCESS run of the same
    name on one SHA; GitHub treats them as one logical check whose state is
    the newest run's (2026-09-04 #1636)."""
    stale = status_check_rollup_entry(
        "backend serial (flaky)",
        "CANCELLED",
        workflow="CI",
        completed_at="2026-09-03T17:25:42Z",
    )
    fresh = status_check_rollup_entry(
        "backend serial (flaky)",
        "SUCCESS",
        workflow="CI",
        completed_at="2026-09-03T18:04:17Z",
    )
    host = gh([status_check_rollup_entry("backend (pytest + pyright)", "SUCCESS"), stale, fresh])
    has_workflows(True)
    r = status.check_ci("1", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.ALL_PASSED
    assert r.failed == []
    assert r.completed.count("backend serial (flaky)") == 1


def test_empty_rollup(gh: Any, has_workflows: Any) -> None:
    """Empty rollup and the runs probe confirms nothing is scheduled: the real
    NO_CHECKS."""
    host = gh([])
    has_workflows(True)
    assert status.check_ci("1", host=host, clock=TEST_CLOCK).verdict is CIStatus.NO_CHECKS


def test_empty_rollup_with_a_queued_run_is_pending_not_no_checks(gh: Any) -> None:
    """2026-09-12, PR #2249 (task #3158): two seconds after a push the rollup was
    still empty while Actions was moments from attaching its checks (the run read
    `in_progress` ten seconds later). An empty rollup with a run in flight is the
    just-pushed attach window, not a settled "no checks" — a watcher reading
    NO_CHECKS here exits before the suite has begun."""
    host = gh([], scheduled=["CI"])

    r = status.check_ci("2249", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.PENDING
    assert r.pending == ["CI"]


def test_empty_rollup_with_an_unanswerable_probe_is_error(
    gh: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runs probe failing must not read as a settled NO_CHECKS — the exact
    verdict this probe exists to rule out when checks may still be attaching."""
    host = gh([])
    monkeypatch.setattr(status, "runs_not_yet_reporting", lambda *_a, **_k: None)

    r = status.check_ci("2249", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.ERROR
    assert "probe" in r.error_detail


def test_gh_failure_is_an_error() -> None:
    host = fake_host(
        lambda argv, **_k: subprocess.CompletedProcess(list(argv), 1, "", "gh: not authenticated")
    )
    r = status.check_ci("1", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.ERROR
    assert "not authenticated" in r.error_detail


# --- the real repo ---


def test_this_repo_has_workflows() -> None:
    """The guard is only armed where workflows exist; here they do."""
    assert status.repo_has_workflows() is True


# --- the false negative: scheduled but not yet reporting ---
# The mirror of the regression above. Between a push and the first check-run appearing on the
# commit, the rollup looks exactly like "Actions never ran" — and that window lands on the first
# poll after a push, which is when an agent is most likely to be watching. Reporting DID NOT RUN
# there sends it off to investigate a CI that is simply still starting.


def test_queued_run_with_no_check_yet_is_pending_not_did_not_run(
    gh: Any, has_workflows: Any
) -> None:
    host = gh([APP_CHECK], scheduled=["CI"])
    has_workflows(True)

    r = status.check_ci("901", host=host, clock=TEST_CLOCK)
    assert r.verdict == CIStatus.PENDING
    assert "CI" in r.pending
    assert "DID NOT RUN" not in r.summary()


def test_partial_suite_green_with_a_run_still_queued_is_pending(
    gh: Any, has_workflows: Any
) -> None:
    """2026-09-10, MonsoraV2 #774: guardrails / e2e / contracts had attached and
    passed while the main CI run was still queued and had attached nothing. The
    rollup alone looked complete and green; only the runs API shows the missing
    remainder. This is the early-green window — part of the suite is in, the
    rest is still coming."""
    host = gh(
        [
            status_check_rollup_entry("guardrails (impacted)", "SUCCESS"),
            status_check_rollup_entry("e2e (smoke)", "SUCCESS"),
            status_check_rollup_entry("contracts", "SUCCESS"),
        ],
        scheduled=["CI"],
    )
    has_workflows(True)

    r = status.check_ci("774", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.PENDING
    assert "CI" in r.pending
    assert "guardrails (impacted)" in r.passed
    host = gh(
        [
            status_check_rollup_entry("guardrails (impacted)", "SUCCESS"),
            status_check_rollup_entry("e2e (smoke)", "SUCCESS"),
            status_check_rollup_entry("contracts", "SUCCESS"),
        ],
        scheduled=[],
        main_completed=True,
    )
    assert status.check_ci("775", host=host, clock=TEST_CLOCK).verdict is CIStatus.ALL_PASSED


def test_partial_suite_green_with_an_unanswerable_probe_is_error(
    gh: Any, has_workflows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closed asymmetry (QA review, 2026-09-11): every attached check passed but
    the probe cannot confirm nothing is still queued. None must not read as
    green — ERROR (unknown) makes --wait print the reason and exit 3 when it
    persists, instead of ALL_PASSED or a fabricated PENDING."""
    host = gh(
        [
            status_check_rollup_entry("guardrails (impacted)", "SUCCESS"),
            status_check_rollup_entry("e2e (smoke)", "SUCCESS"),
        ]
    )
    has_workflows(True)
    monkeypatch.setattr(status, "runs_not_yet_reporting", lambda *_a, **_k: None)

    r = status.check_ci("776", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.ERROR
    assert "probe" in r.error_detail


def test_early_green_window_without_the_main_run_is_not_green(gh: Any, has_workflows: Any) -> None:
    """PR #3137 had only passing app/proof checks before CI registered; wait it
    out until the main run is visible rather than calling that narrow window green."""
    checks = [
        APP_CHECK,
        status_check_rollup_entry("prove-example-a", "SKIPPED", workflow="Example proof A"),
        status_check_rollup_entry("prove-example-b", "SKIPPED", workflow="Example proof B"),
    ]
    has_workflows(True)
    host = gh(checks, scheduled=[], main_completed=False)

    r = status.check_ci("3137", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.PENDING
    assert status.MAIN_CI_WORKFLOW_NAME in r.pending
    assert "all green" not in r.summary()
    assert "pending" in r.summary().lower()
    host = gh(checks, scheduled=[], main_completed=True)
    assert status.check_ci("3137", host=host, clock=TEST_CLOCK).verdict is CIStatus.ALL_PASSED


def test_main_workflow_probe_failure_is_error(
    gh: Any, has_workflows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unanswerable main-workflow probe is unknown, never a green verdict."""
    host = gh([status_check_rollup_entry("backend", "SUCCESS")], scheduled=[], main_completed=True)
    has_workflows(True)
    monkeypatch.setattr(status, "main_workflow_run_completed", lambda *_a, **_k: None)

    r = status.check_ci("3137", host=host, clock=TEST_CLOCK)
    assert r.verdict is CIStatus.ERROR
    assert "probe" in r.error_detail


def test_main_workflow_probe_reads_completed_runs_of_the_main_workflow() -> None:
    """The completed-runs query is head-specific and filters for the main workflow."""
    seen: dict[str, list[str]] = {}
    state = {"stdout": "2"}

    def _run(argv: Sequence[str], *_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        seen["cmd"] = argv
        return subprocess.CompletedProcess(argv, 0, state["stdout"], "")

    host = fake_host(_run)
    assert status.main_workflow_run_completed("abc123", None, host=host) is True
    joined = " ".join(seen["cmd"])
    assert "head_sha=abc123" in joined
    assert "status=completed" in joined
    assert 'select(.name == "CI" and .conclusion != "skipped")' in joined
    state["stdout"] = "0"
    assert status.main_workflow_run_completed("abc123", None, host=host) is False


def core_rollup(conclusion: str, *, classify: str = "SKIPPED") -> list[dict]:
    return [
        APP_CHECK,
        status_check_rollup_entry("classify change", classify),
        status_check_rollup_entry("backend shard (${{ matrix.group }}/16)", conclusion),
        status_check_rollup_entry("e2e shard (${{ matrix.group }}/4)", conclusion),
        status_check_rollup_entry("backend selected subset", "SKIPPED"),
        status_check_rollup_entry("CI summary", "SKIPPED"),
    ]


def test_draft_pr_with_skipped_core_suite_is_not_ready(gh: Any, has_workflows: Any) -> None:
    host = gh(core_rollup("SKIPPED"), is_draft=True, scheduled=[], main_completed=True)
    has_workflows(True)
    result = status.check_ci("3313", host=host, clock=TEST_CLOCK)
    assert result.verdict is CIStatus.NOT_READY
    assert "NOT READY" in result.summary() and "draft" in result.summary()
    assert "all green" not in result.summary()
    assert result.core_skipped == [c["name"] for c in core_rollup("SKIPPED")[2:4]]


def test_draft_pr_whose_core_suite_ran_stays_green(gh: Any, has_workflows: Any) -> None:
    host = gh(core_rollup("SUCCESS"), is_draft=True, main_completed=True)
    has_workflows(True)
    assert status.check_ci("3313", host=host, clock=TEST_CLOCK).verdict is CIStatus.ALL_PASSED


def test_ready_transition_with_draft_skipped_core_is_pending(gh: Any, has_workflows: Any) -> None:
    host = gh(core_rollup("SKIPPED"), is_draft=False, scheduled=[], main_completed=False)
    has_workflows(True)
    result = status.check_ci("3313", host=host, clock=TEST_CLOCK)
    assert result.verdict is CIStatus.PENDING
    assert status.MAIN_CI_WORKFLOW_NAME in result.pending
    assert "skipped by draft gating" in result.summary()
    assert result.core_skipped and "all green" not in result.summary()


def test_path_filtered_core_skip_with_a_ran_main_run_stays_green(
    gh: Any, has_workflows: Any
) -> None:
    checks = [
        *core_rollup("SKIPPED", classify="SUCCESS"),
        status_check_rollup_entry("repo language (no raw CJK)", "SUCCESS"),
    ]
    host = gh(checks, is_draft=False, main_completed=True)
    has_workflows(True)
    assert status.check_ci("2505", host=host, clock=TEST_CLOCK).verdict is CIStatus.ALL_PASSED


def test_core_suite_skipped_reads_only_the_shard_families() -> None:
    skipped = core_rollup("SKIPPED")
    names = [c["name"] for c in skipped[2:4]]
    assert status.core_suite_skipped([]) is None
    assert status.core_suite_skipped([APP_CHECK]) is None
    assert (
        status.core_suite_skipped([status_check_rollup_entry("backend selected subset", "SKIPPED")])
        is None
    )
    assert status.core_suite_skipped(skipped) == names
    assert (
        status.core_suite_skipped(
            [*skipped, status_check_rollup_entry("backend shard (1/16)", "SUCCESS")]
        )
        is None
    )


def test_main_workflow_probe_returns_none_when_unanswerable() -> None:
    """A failed or unparseable completed-run query is unknown, not no main run."""
    state = {"rc": 1, "stdout": ""}

    def _run(argv: Sequence[str], *_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        returncode = state["rc"]
        assert isinstance(returncode, int)
        return subprocess.CompletedProcess(list(argv), returncode, str(state["stdout"]), "boom")

    host = fake_host(_run)
    assert status.main_workflow_run_completed("abc123", None, host=host) is None
    state["rc"] = 0
    state["stdout"] = "not a number"
    assert status.main_workflow_run_completed("abc123", None, host=host) is None


def test_runs_api_failure_keeps_the_conservative_verdict(
    gh: Any, has_workflows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that cannot answer must not invent a reason to wait — an
    unreachable API is not evidence that CI is coming. Without any attached
    workflow check the not-green NO_WORKFLOW_RUNS verdict is kept, exactly as
    when the probe answered []; only the all-passed path (below) has to tell
    None from []."""
    host = gh([APP_CHECK])
    has_workflows(True)
    monkeypatch.setattr(status, "runs_not_yet_reporting", lambda *_a, **_k: None)

    assert status.check_ci("886", host=host, clock=TEST_CLOCK).verdict == CIStatus.NO_WORKFLOW_RUNS


def completed_process_stub(stdout: str = "", returncode: int = 0) -> Any:
    return subprocess.CompletedProcess([], returncode, stdout, "boom")


@pytest.fixture
def no_sleep() -> Clock:
    return make_clock()


@pytest.fixture
def poll(monkeypatch: pytest.MonkeyPatch):
    """Stub check_ci with a queue of verdicts; main() consumes them in order,
    the last one repeating forever (a settled verdict ends the loop)."""

    def _install(*verdicts: CIStatus) -> None:
        calls = {"n": 0}

        def fake_check(
            pr: str | int,
            *,
            repo: str | None = None,
            host: object = None,
            clock: object = None,
        ) -> status.CIResult:
            del pr, repo, host, clock
            i = min(calls["n"], len(verdicts) - 1)
            calls["n"] += 1
            v = verdicts[i]
            if v is CIStatus.ALL_PASSED:
                return status.CIResult(verdict=v, passed=["lint", "test"])
            if v is CIStatus.FAILED:
                return status.CIResult(
                    verdict=v, failed=[{"name": "lint", "conclusion": "FAILURE"}]
                )
            if v is CIStatus.ERROR:
                return status.CIResult(verdict=v, error_detail="gh CLI error: boom")
            if v is CIStatus.NOT_READY:
                return status.CIResult(verdict=v, core_skipped=["backend shard (1/16)"])
            return status.CIResult(verdict=v)

        monkeypatch.setattr(status, "check_ci", fake_check)

    return _install


# --- is_terminal: watchers must never guess verdict strings -------------------
# 2026-08-03: a watcher hard-coded ("success", "failure", "merged") — none of
# which exist — and spun silently forever while CI went green. `is_terminal`
# gives watchers a predicate instead of a string to mistype.


# --- --rerun-failed-jobs CLI (issue #102) ---


# --- Trunk queue operator commands: --queue-status / --evict ---


# --- --diagnose: Trunk/CI failure triage (task #2572) ---


@pytest.fixture
def diag_gh() -> Any:
    """Stub the gh/git subprocess for --diagnose: canned responses keyed by a
    substring of the command line, consumed in call order (first entry whose
    key matches the current call)."""

    def _install(responses: list[tuple[str, str]]) -> Host:
        def run(argv: Sequence[str], *_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            argv = list(argv)
            joined = " ".join(str(c) for c in argv)
            for idx, (key, out) in enumerate(responses):
                if key in joined:
                    responses.pop(idx)
                    return subprocess.CompletedProcess(argv, 0, out, "")
            return subprocess.CompletedProcess(argv, 1, "", "unmatched: " + joined)

        return fake_host(run)

    return _install


def diag_pr_view(mergeable: str = "MERGEABLE", checks: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "mergeable": mergeable,
            "headRefOid": "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111",
            "state": "OPEN",
            "statusCheckRollup": checks or [],
        }
    )


def diag_check(name: str, conclusion: str = "FAILURE") -> dict:
    return {"name": name, "conclusion": conclusion, "status": "COMPLETED"}


def diag_job(name: str, job_id: int = 9, conclusion: str = "FAILURE") -> dict:
    """Real-shaped REST job object: numeric `.id`, lowercase conclusion."""
    return {"name": name, "id": job_id, "run_id": 10, "conclusion": conclusion.lower()}


def diag_runs(run_id: int = 10) -> str:
    """Real-shaped REST runs payload with one CI run (issue #1945).

    The run carries `status: completed`, which `job_rerun` reads to decide whether a re-run is
    admissible (task #3764)."""
    return json.dumps(
        {
            "total_count": 1,
            "workflow_runs": [
                {
                    "id": run_id,
                    "name": "CI",
                    "created_at": "2026-09-09T10:00:00Z",
                    "status": "completed",
                }
            ],
        }
    )


def diag_jobs_payload(job: dict) -> str:
    """Real-shaped REST jobs payload wrapping one projected job (issue #1945)."""
    return json.dumps({"total_count": 1, "jobs": [job]})


# --- --ci-usage: per-agent CI minute rollup (task #2575) ---


# --- GitHub-limbo runs (task #3275, 2026-09-13): `queued` with zero jobs, aged past
# `_LIMBO_AGE_SECONDS`; no GitHub API can clean them up (cancel -> 409, rerun -> 403, DELETE -> 403).
# They held all-green rollups PENDING until a --wait budget silently ran out. ---


def aged(seconds: float = 900.0) -> str:
    """A GitHub RFC3339 `created_at` that is `seconds` old."""
    return (TEST_NOW - timedelta(seconds=seconds)).isoformat()


def install_probe(
    runs: object,
    jobs: dict[int, int],
    *,
    calls: list[list[str]] | None = None,
    runs_rc: int = 0,
    jobs_rc: dict[int, int] | None = None,
) -> Host:
    """Stub `gh api` for `_limbo_runs`: one runs probe, then one jobs probe per
    aged queued candidate. `runs` is the jq-projected list (or a raw string
    standing in for unreadable output); `jobs` maps a run id to its jq-printed
    total_count."""

    def run(argv: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        if calls is not None:
            calls.append(argv)
        url = argv[2]
        if "/jobs" in url:
            run_id = int(url.split("/")[-2])
            return subprocess.CompletedProcess(
                argv, (jobs_rc or {}).get(run_id, 0), str(jobs[run_id]), ""
            )
        return subprocess.CompletedProcess(
            argv, runs_rc, runs if isinstance(runs, str) else json.dumps(runs), ""
        )

    return fake_host(run)


def limbo_result(
    *,
    pending: list[str] | None = None,
    limbo: list[status.LimboRun] | None = None,
    failed: list[dict[str, str]] | None = None,
) -> Any:
    return status.CIResult(
        verdict=CIStatus.PENDING,
        pending=list(pending or []),
        limbo=list(limbo or []),
        failed=list(failed or []),
    )


def install_results_poller(monkeypatch: pytest.MonkeyPatch, *results: Any) -> None:
    calls = {"n": 0}

    def fake_check(
        pr: str | int,
        *,
        repo: str | None = None,
        host: object = None,
        clock: object = None,
    ) -> Any:
        del pr, repo, host, clock
        i = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        return results[i]

    monkeypatch.setattr(status, "check_ci", fake_check)
