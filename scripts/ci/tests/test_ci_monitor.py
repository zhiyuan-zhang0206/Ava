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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scripts.ci import status

# Untyped fixtures and helper calls throughout: the call-site rules stay at warning for this file.
# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning

CIStatus = status.CIStatus


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


def _gh_runner(calls: list[list[str]]):
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    return run


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch):
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
    ) -> None:
        rollup = json.dumps(
            {
                "mergeable": mergeable,
                "statusCheckRollup": checks,
                "headRefOid": "deadbeef",
                "isDraft": is_draft,
            }
        )
        runs = json.dumps(scheduled or [])

        def _run(cmd, *_a, **_k):
            url = cmd[2] if len(cmd) > 2 else ""
            if "api" not in cmd:
                stdout = rollup
            elif "status=completed" in url:
                # The main-workflow-visibility probe: `length` of the completed
                # main-workflow runs for this head.
                stdout = "1" if main_completed else "0"
            elif "actions/runs?" in url and "--jq" not in cmd:
                # job_rerun reads the raw REST runs payload (issue #1945);
                # keep its view empty so diagnose tests stay deterministic.
                stdout = json.dumps({"total_count": 0, "workflow_runs": []})
            elif "/jobs?" in url:
                stdout = json.dumps({"total_count": 0, "jobs": []})
            else:
                stdout = runs

            class _R:
                def __init__(self) -> None:
                    self.returncode = 0
                    self.stderr = ""
                    self.stdout = stdout

            return _R()

        monkeypatch.setattr(status.subprocess, "run", _run)

    return _install


@pytest.fixture
def has_workflows(monkeypatch: pytest.MonkeyPatch):
    def _set(value: bool) -> None:
        monkeypatch.setattr(status, "_repo_has_workflows", lambda: value)

    return _set


# --- the regression this file exists for ---


def test_app_check_alone_is_not_green(gh: Any, has_workflows: Any) -> None:
    """The 2026-07-28 shape: Actions never ran, an app check passed."""
    gh([_APP_CHECK], scheduled=[])
    has_workflows(True)
    r = status.check_ci("886")
    assert r.verdict is CIStatus.NO_WORKFLOW_RUNS
    assert r.workflow_checks == []
    assert "DID NOT RUN" in r.summary()


def test_app_check_alone_is_green_when_repo_has_no_workflows(gh: Any, has_workflows: Any) -> None:
    """A repo with no workflow files is legitimately green on app checks alone."""
    gh([_APP_CHECK])
    has_workflows(False)
    assert status.check_ci("1").verdict is CIStatus.ALL_PASSED


def test_one_workflow_check_is_enough_to_be_green(gh: Any, has_workflows: Any) -> None:
    """The guard asks whether the suite ran at all — not how many jobs it has."""
    gh([_APP_CHECK, _check("backend (pytest + pyright)", "SUCCESS")])
    has_workflows(True)
    r = status.check_ci("1")
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
    r = status.check_ci("1")
    assert r.verdict is CIStatus.ALL_PASSED
    assert len(r.passed) == 4


def test_failure_wins_over_the_workflow_guard(gh: Any, has_workflows: Any) -> None:
    """A real failure must report FAILED, never the did-not-run verdict."""
    gh([_APP_CHECK, _check("backend (pytest + pyright)", "FAILURE")])
    has_workflows(True)
    r = status.check_ci("1")
    assert r.verdict is CIStatus.FAILED
    assert r.failed == [{"name": "backend (pytest + pyright)", "conclusion": "FAILURE"}]


def test_pending_wins_over_the_workflow_guard(gh: Any, has_workflows: Any) -> None:
    """Still-running checks mean wait, not did-not-run."""
    gh([_APP_CHECK, _check("backend", "", status="IN_PROGRESS")])
    has_workflows(True)
    assert status.check_ci("1").verdict is CIStatus.PENDING


def test_unknown_conclusion_counts_as_pending(gh: Any, has_workflows: Any) -> None:
    """A COMPLETED check with an unrecognized conclusion must not read as green."""
    gh([_check("weird", "SOMETHING_NEW")])
    has_workflows(True)
    r = status.check_ci("1")
    assert r.verdict is CIStatus.PENDING
    assert r.pending == ["weird"]


def test_merge_conflict_short_circuits(gh: Any, has_workflows: Any) -> None:
    gh([_check("backend", "SUCCESS")], mergeable="CONFLICTING")
    has_workflows(True)
    assert status.check_ci("1").verdict is CIStatus.MERGE_CONFLICT


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
    gh([_check("backend (pytest + pyright)", "SUCCESS"), status_ctx])
    has_workflows(True)
    result = status.check_ci("1")
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
    stale = _check(
        "backend serial (flaky)",
        "CANCELLED",
        workflow="CI",
        completed_at="2026-09-03T17:25:42Z",
    )
    fresh = _check(
        "backend serial (flaky)",
        "SUCCESS",
        workflow="CI",
        completed_at="2026-09-03T18:04:17Z",
    )
    gh([_check("backend (pytest + pyright)", "SUCCESS"), stale, fresh])
    has_workflows(True)
    r = status.check_ci("1")
    assert r.verdict is CIStatus.ALL_PASSED
    assert r.failed == []
    assert r.completed.count("backend serial (flaky)") == 1


def test_empty_rollup(gh: Any, has_workflows: Any) -> None:
    """Empty rollup and the runs probe confirms nothing is scheduled: the real
    NO_CHECKS."""
    gh([])
    has_workflows(True)
    assert status.check_ci("1").verdict is CIStatus.NO_CHECKS


def test_empty_rollup_with_a_queued_run_is_pending_not_no_checks(gh: Any) -> None:
    """2026-09-12, PR #2249 (task #3158): two seconds after a push the rollup was
    still empty while Actions was moments from attaching its checks (the run read
    `in_progress` ten seconds later). An empty rollup with a run in flight is the
    just-pushed attach window, not a settled "no checks" — a watcher reading
    NO_CHECKS here exits before the suite has begun."""
    gh([], scheduled=["CI"])

    r = status.check_ci("2249")
    assert r.verdict is CIStatus.PENDING
    assert r.pending == ["CI"]


def test_empty_rollup_with_an_unanswerable_probe_is_error(
    gh: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runs probe failing must not read as a settled NO_CHECKS — the exact
    verdict this probe exists to rule out when checks may still be attaching."""
    gh([])
    monkeypatch.setattr(status, "_runs_not_yet_reporting", lambda *_a, **_k: None)

    r = status.check_ci("2249")
    assert r.verdict is CIStatus.ERROR
    assert "probe" in r.error_detail


def test_gh_failure_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _R:
        returncode = 1
        stdout = ""
        stderr = "gh: not authenticated"

    monkeypatch.setattr(status.subprocess, "run", lambda *_a, **_k: _R())
    r = status.check_ci("1")
    assert r.verdict is CIStatus.ERROR
    assert "not authenticated" in r.error_detail


# --- the real repo ---


def test_this_repo_has_workflows() -> None:
    """The guard is only armed where workflows exist; here they do."""
    assert status._repo_has_workflows() is True


# --- the false negative: scheduled but not yet reporting ---
# The mirror of the regression above. Between a push and the first check-run appearing on the
# commit, the rollup looks exactly like "Actions never ran" — and that window lands on the first
# poll after a push, which is when an agent is most likely to be watching. Reporting DID NOT RUN
# there sends it off to investigate a CI that is simply still starting.


def test_queued_run_with_no_check_yet_is_pending_not_did_not_run(
    gh: Any, has_workflows: Any
) -> None:
    gh([_APP_CHECK], scheduled=["CI"])
    has_workflows(True)

    r = status.check_ci("901")
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
    gh(
        [
            _check("guardrails (impacted)", "SUCCESS"),
            _check("e2e (smoke)", "SUCCESS"),
            _check("contracts", "SUCCESS"),
        ],
        scheduled=["CI"],
    )
    has_workflows(True)

    r = status.check_ci("774")
    assert r.verdict is CIStatus.PENDING
    assert "CI" in r.pending
    assert "guardrails (impacted)" in r.passed
    gh(
        [
            _check("guardrails (impacted)", "SUCCESS"),
            _check("e2e (smoke)", "SUCCESS"),
            _check("contracts", "SUCCESS"),
        ],
        scheduled=[],
        main_completed=True,
    )
    assert status.check_ci("775").verdict is CIStatus.ALL_PASSED


def test_partial_suite_green_with_an_unanswerable_probe_is_error(
    gh: Any, has_workflows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closed asymmetry (QA review, 2026-09-11): every attached check passed but
    the probe cannot confirm nothing is still queued. None must not read as
    green — ERROR (unknown) makes --wait print the reason and exit 3 when it
    persists, instead of ALL_PASSED or a fabricated PENDING."""
    gh(
        [
            _check("guardrails (impacted)", "SUCCESS"),
            _check("e2e (smoke)", "SUCCESS"),
        ]
    )
    has_workflows(True)
    monkeypatch.setattr(status, "_runs_not_yet_reporting", lambda *_a, **_k: None)

    r = status.check_ci("776")
    assert r.verdict is CIStatus.ERROR
    assert "probe" in r.error_detail


def test_early_green_window_without_the_main_run_is_not_green(gh: Any, has_workflows: Any) -> None:
    """PR #3137 had only passing app/proof checks before CI registered; wait it
    out until the main run is visible rather than calling that narrow window green."""
    checks = [
        _APP_CHECK,
        _check("prove-example-a", "SKIPPED", workflow="Example proof A"),
        _check("prove-example-b", "SKIPPED", workflow="Example proof B"),
    ]
    has_workflows(True)
    gh(checks, scheduled=[], main_completed=False)

    r = status.check_ci("3137")
    assert r.verdict is CIStatus.PENDING
    assert status.MAIN_CI_WORKFLOW_NAME in r.pending
    assert "all green" not in r.summary()
    assert "pending" in r.summary().lower()
    gh(checks, scheduled=[], main_completed=True)
    assert status.check_ci("3137").verdict is CIStatus.ALL_PASSED


def test_main_workflow_probe_failure_is_error(
    gh: Any, has_workflows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unanswerable main-workflow probe is unknown, never a green verdict."""
    gh([_check("backend", "SUCCESS")], scheduled=[], main_completed=True)
    has_workflows(True)
    monkeypatch.setattr(status, "_main_workflow_run_completed", lambda *_a, **_k: None)

    r = status.check_ci("3137")
    assert r.verdict is CIStatus.ERROR
    assert "probe" in r.error_detail


def test_main_workflow_probe_reads_completed_runs_of_the_main_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completed-runs query is head-specific and filters for the main workflow."""
    seen: dict[str, list[str]] = {}

    class _R:
        returncode = 0
        stdout = "2"
        stderr = ""

    def _run(cmd, *_a, **_k):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(status.subprocess, "run", _run)
    assert status._main_workflow_run_completed("abc123", None) is True
    joined = " ".join(seen["cmd"])
    assert "head_sha=abc123" in joined
    assert "status=completed" in joined
    assert 'select(.name == "CI" and .conclusion != "skipped")' in joined
    _R.stdout = "0"
    assert status._main_workflow_run_completed("abc123", None) is False


def _core_rollup(conclusion: str, *, classify: str = "SKIPPED") -> list[dict]:
    return [
        _APP_CHECK,
        _check("classify change", classify),
        _check("backend shard (${{ matrix.group }}/16)", conclusion),
        _check("e2e shard (${{ matrix.group }}/4)", conclusion),
        _check("backend selected subset", "SKIPPED"),
        _check("CI summary", "SKIPPED"),
    ]


def test_draft_pr_with_skipped_core_suite_is_not_ready(gh: Any, has_workflows: Any) -> None:
    gh(_core_rollup("SKIPPED"), is_draft=True, scheduled=[], main_completed=True)
    has_workflows(True)
    result = status.check_ci("3313")
    assert result.verdict is CIStatus.NOT_READY
    assert "NOT READY" in result.summary() and "draft" in result.summary()
    assert "all green" not in result.summary()
    assert result.core_skipped == [c["name"] for c in _core_rollup("SKIPPED")[2:4]]


def test_draft_pr_whose_core_suite_ran_stays_green(gh: Any, has_workflows: Any) -> None:
    gh(_core_rollup("SUCCESS"), is_draft=True, main_completed=True)
    has_workflows(True)
    assert status.check_ci("3313").verdict is CIStatus.ALL_PASSED


def test_ready_transition_with_draft_skipped_core_is_pending(gh: Any, has_workflows: Any) -> None:
    gh(_core_rollup("SKIPPED"), is_draft=False, scheduled=[], main_completed=False)
    has_workflows(True)
    result = status.check_ci("3313")
    assert result.verdict is CIStatus.PENDING
    assert status.MAIN_CI_WORKFLOW_NAME in result.pending
    assert "skipped by draft gating" in result.summary()
    assert result.core_skipped and "all green" not in result.summary()


def test_path_filtered_core_skip_with_a_ran_main_run_stays_green(
    gh: Any, has_workflows: Any
) -> None:
    checks = [
        *_core_rollup("SKIPPED", classify="SUCCESS"),
        _check("repo language (no raw CJK)", "SUCCESS"),
    ]
    gh(checks, is_draft=False, main_completed=True)
    has_workflows(True)
    assert status.check_ci("2505").verdict is CIStatus.ALL_PASSED


def test_core_suite_skipped_reads_only_the_shard_families() -> None:
    skipped = _core_rollup("SKIPPED")
    names = [c["name"] for c in skipped[2:4]]
    assert status._core_suite_skipped([]) is None
    assert status._core_suite_skipped([_APP_CHECK]) is None
    assert status._core_suite_skipped([_check("backend selected subset", "SKIPPED")]) is None
    assert status._core_suite_skipped(skipped) == names
    assert status._core_suite_skipped([*skipped, _check("backend shard (1/16)", "SUCCESS")]) is None


def test_main_workflow_probe_returns_none_when_unanswerable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed or unparseable completed-run query is unknown, not no main run."""

    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(status.subprocess, "run", lambda *_a, **_k: _R())
    assert status._main_workflow_run_completed("abc123", None) is None
    _R.returncode = 0
    _R.stdout = "not a number"
    assert status._main_workflow_run_completed("abc123", None) is None


def test_runs_api_failure_keeps_the_conservative_verdict(
    gh: Any, has_workflows: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that cannot answer must not invent a reason to wait — an
    unreachable API is not evidence that CI is coming. Without any attached
    workflow check the not-green NO_WORKFLOW_RUNS verdict is kept, exactly as
    when the probe answered []; only the all-passed path (below) has to tell
    None from []."""
    gh([_APP_CHECK])
    has_workflows(True)
    monkeypatch.setattr(status, "_runs_not_yet_reporting", lambda *_a, **_k: None)

    assert status.check_ci("886").verdict == CIStatus.NO_WORKFLOW_RUNS


def _completed(stdout: str = "", returncode: int = 0) -> Any:
    return status.subprocess.CompletedProcess([], returncode, stdout, "boom")


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status.time, "sleep", lambda _s: None)


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

        monkeypatch.setattr(status.subprocess, "run", run)

    return _install


def _diag_pr_view(mergeable: str = "MERGEABLE", checks: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "mergeable": mergeable,
            "headRefOid": "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111",
            "state": "OPEN",
            "statusCheckRollup": checks or [],
        }
    )


def _diag_check(name: str, conclusion: str = "FAILURE") -> dict:
    return {"name": name, "conclusion": conclusion, "status": "COMPLETED"}


def _diag_job(name: str, job_id: int = 9, conclusion: str = "FAILURE") -> dict:
    """Real-shaped REST job object: numeric `.id`, lowercase conclusion."""
    return {"name": name, "id": job_id, "run_id": 10, "conclusion": conclusion.lower()}


def _diag_runs(run_id: int = 10) -> str:
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


def _diag_jobs_payload(job: dict) -> str:
    """Real-shaped REST jobs payload wrapping one projected job (issue #1945)."""
    return json.dumps({"total_count": 1, "jobs": [job]})


# --- --ci-usage: per-agent CI minute rollup (task #2575) ---


# --- GitHub-limbo runs (task #3275, 2026-09-13): `queued` with zero jobs, aged past
# `_LIMBO_AGE_SECONDS`; no GitHub API can clean them up (cancel -> 409, rerun -> 403, DELETE -> 403).
# They held all-green rollups PENDING until a --wait budget silently ran out. ---


class _ProbeResponse:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _aged(seconds: float = 900.0) -> str:
    """A GitHub RFC3339 `created_at` that is `seconds` old."""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _install_probe(
    monkeypatch: pytest.MonkeyPatch,
    runs: object,
    jobs: dict[int, int],
    *,
    calls: list[list[str]] | None = None,
    runs_rc: int = 0,
    jobs_rc: dict[int, int] | None = None,
) -> None:
    """Stub `gh api` for `_limbo_runs`: one runs probe, then one jobs probe per
    aged queued candidate. `runs` is the jq-projected list (or a raw string
    standing in for unreadable output); `jobs` maps a run id to its jq-printed
    total_count."""

    def run(cmd, **_kwargs):
        if calls is not None:
            calls.append(list(cmd))
        url = cmd[2]
        if "/jobs" in url:
            run_id = int(url.split("/")[-2])
            return _ProbeResponse((jobs_rc or {}).get(run_id, 0), str(jobs[run_id]))
        return _ProbeResponse(runs_rc, runs if isinstance(runs, str) else json.dumps(runs))

    monkeypatch.setattr(status.subprocess, "run", run)


def _limbo_result(
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


def _install_results_poller(monkeypatch: pytest.MonkeyPatch, *results: Any) -> None:
    calls = {"n": 0}

    def fake_check(pr, *, repo):
        i = min(calls["n"], len(results) - 1)
        calls["n"] += 1
        return results[i]

    monkeypatch.setattr(status, "check_ci", fake_check)
