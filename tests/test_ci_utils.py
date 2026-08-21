"""Tests for scripts/ci_utils.py — the tool AGENTS.md gates merges on.

The verdict this file cares most about is the one that was wrong on 2026-07-28:
when Actions cannot schedule (#885 switched to hosted runners a private repo has
no minutes for), every workflow check vanishes from the rollup, the only check
left is a GitHub App's, it passes, and `check_ci` reported "CI all green (1
checks passed)" on a pull request that ran nothing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

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


def _check(name: str, conclusion: str, *, workflow: str = "CI", status: str = "COMPLETED") -> dict:
    """One statusCheckRollup entry. `workflow=""` models a GitHub App check."""
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "workflowName": workflow,
    }


_APP_CHECK = _check("GitGuardian Security Checks", "SUCCESS", workflow="")


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


# --- issue #98: the Mergify Merge Queue check is queue state, not CI ----------
# `--wait --merge` deadlocked when a PR was dequeued (e.g. by a force-push):
# the only non-green check left was "Mergify Merge Queue" itself, and nothing
# but re-queuing could ever turn it green — but queuing is what --merge does
# once it sees green. Fix: (a) exclude checks named "Mergify*" from the
# all-green predicate; (b) detect a dequeue and post `requeue` instead of
# `queue`.

_MERGIFY_PENDING = _check("Mergify Merge Queue", "", workflow="", status="IN_PROGRESS")
_MERGIFY_SUCCESS = _check("Mergify Merge Queue", "SUCCESS", workflow="")


def test_pending_mergify_check_does_not_block_all_passed(gh: Any, has_workflows: Any) -> None:
    """The reproduction from issue #98: a dequeued PR's Mergify check sits
    IN_PROGRESS forever. It must not keep real, completed CI from reading
    green."""
    gh([_check("backend (pytest + pyright)", "SUCCESS"), _MERGIFY_PENDING])
    has_workflows(True)
    r = ci_utils.check_ci("53")
    assert r.verdict is CIStatus.ALL_PASSED
    assert "Mergify Merge Queue" not in r.pending


def test_mergify_check_excluded_from_passed_and_workflow_checks(
    gh: Any, has_workflows: Any
) -> None:
    """Even a green Mergify check must not count as a CI check — it is queue
    state, not a workflow result."""
    gh([_check("backend (pytest + pyright)", "SUCCESS"), _MERGIFY_SUCCESS])
    has_workflows(True)
    r = ci_utils.check_ci("56")
    assert r.verdict is CIStatus.ALL_PASSED
    assert "Mergify Merge Queue" not in r.passed
    assert "Mergify Merge Queue" not in r.workflow_checks
    assert len(r.mergify_checks) == 1


def test_mergify_only_rollup_falls_back_to_no_workflow_runs(gh: Any, has_workflows: Any) -> None:
    """Stripping the Mergify check must not silently invent an ALL_PASSED
    verdict for a PR with no real CI checks at all — the existing
    NO_WORKFLOW_RUNS guard still applies to what's left."""
    gh([_MERGIFY_SUCCESS], scheduled=[])
    has_workflows(True)
    assert ci_utils.check_ci("1").verdict is CIStatus.NO_WORKFLOW_RUNS


def test_mergify_was_dequeued_recognizes_removal_text() -> None:
    assert ci_utils._mergify_was_dequeued("The pull request has been removed from the queue")
    assert ci_utils._mergify_was_dequeued("The pull request has been dequeued")


def test_mergify_was_dequeued_false_for_normal_states() -> None:
    assert not ci_utils._mergify_was_dequeued("Merged via merge queue")
    assert not ci_utils._mergify_was_dequeued("Waiting for conditions to match")
    assert not ci_utils._mergify_was_dequeued(None)
    assert not ci_utils._mergify_was_dequeued("")


def test_fetch_mergify_check_title_reads_jq_output(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, list[str]] = {}

    class _R:
        returncode = 0
        stdout = "The pull request has been removed from the queue\n"
        stderr = ""

    def _run(cmd, *_a, **_k):
        seen["cmd"] = cmd
        return _R()

    monkeypatch.setattr(ci_utils.subprocess, "run", _run)

    title = ci_utils._fetch_mergify_check_title("abc123", "o/r")

    assert title == "The pull request has been removed from the queue"
    joined = " ".join(seen["cmd"])
    assert "repos/o/r/commits/abc123/check-runs" in joined
    assert "Mergify Merge Queue" in joined


def test_fetch_mergify_check_title_none_on_missing_head_sha() -> None:
    assert ci_utils._fetch_mergify_check_title("", "o/r") is None


def test_fetch_mergify_check_title_none_on_gh_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _R:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(ci_utils.subprocess, "run", lambda *_a, **_k: _R())

    assert ci_utils._fetch_mergify_check_title("abc123", "o/r") is None


def test_fetch_mergify_check_title_none_on_jq_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Mergify check on this commit -> the jq selector yields `null`."""

    class _R:
        returncode = 0
        stdout = "null\n"
        stderr = ""

    monkeypatch.setattr(ci_utils.subprocess, "run", lambda *_a, **_k: _R())

    assert ci_utils._fetch_mergify_check_title("abc123", "o/r") is None


def test_queue_command_picks_requeue_after_dequeue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ci_utils,
        "_fetch_mergify_check_title",
        lambda *_a, **_k: "The pull request has been removed from the queue",
    )
    assert ci_utils._queue_command("o/r", "abc123") == "@mergifyio requeue"


def test_queue_command_defaults_to_queue_when_never_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_utils, "_fetch_mergify_check_title", lambda *_a, **_k: None)
    assert ci_utils._queue_command("o/r", "abc123") == "@mergifyio queue"


def test_queue_command_defaults_to_queue_for_normal_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ci_utils, "_fetch_mergify_check_title", lambda *_a, **_k: "Merged via merge queue"
    )
    assert ci_utils._queue_command("o/r", "abc123") == "@mergifyio queue"


# --- --wait (poller) mode: the canonical CI watcher ---------------------------
# `ci_utils.py <PR> --wait` polls check_ci until the verdict settles, then
# exits with the monitor contract (0 green / 1 not green / 3 persistent
# errors / 4 merge failed). check_ci itself is covered above; these tests
# cover the poller loop by stubbing check_ci with a queue of verdicts and
# never sleeping.


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PENDING loop must spin without real waits."""
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


def test_wait_merge_enqueues_when_green(no_sleep, poll, monkeypatch, capsys) -> None:
    poll(CIStatus.ALL_PASSED)
    captured: dict = {}
    # The dequeue-detection decision (_queue_command) is covered by its own
    # unit tests below; stub it here so this test isolates enqueue + merge-wait.
    monkeypatch.setattr(ci_utils, "_queue_command", lambda *_a, **_k: "@mergifyio queue")

    def fake_run(cmd, **_kw):
        class _R:
            returncode: int
            stdout: str
            stderr: str

        # Enqueue (gh pr comment "@mergifyio queue") then the merge-watch
        # poll (gh pr view --json state) reports Mergify landed the PR.
        if cmd[:4] == ["gh", "pr", "comment", "1243"]:
            captured["cmd"] = cmd
            r = _R()
            r.returncode = 0
            r.stdout = "Created comment"
            r.stderr = ""
            return r
        if cmd[:4] == ["gh", "pr", "view", "1243"]:
            r = _R()
            r.returncode = 0
            r.stdout = '{"state": "MERGED"}'
            r.stderr = ""
            return r
        raise AssertionError(f"unexpected gh call: {cmd}")

    monkeypatch.setattr(ci_utils.subprocess, "run", fake_run)
    # --merge implies --wait: enqueue with Mergify, then wait for the queue
    # to land the PR.
    assert ci_utils.main(["1243", "--merge"]) == 0
    assert captured["cmd"][:4] == ["gh", "pr", "comment", "1243"]
    assert "--repo" in captured["cmd"]
    assert "--body" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--body") + 1] == "@mergifyio queue"
    assert "gh pr merge" not in " ".join(captured["cmd"])


def test_wait_merge_failure_exits_four(no_sleep, poll, monkeypatch, capsys) -> None:
    poll(CIStatus.ALL_PASSED)

    def fake_run(cmd, **_kw):
        class _R:
            returncode = 1
            stdout = ""
            stderr = "branch not found"

        return _R()

    monkeypatch.setattr(ci_utils.subprocess, "run", fake_run)
    assert ci_utils.main(["1243", "--merge"]) == 4
    assert "enqueue failed" in capsys.readouterr().err


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
