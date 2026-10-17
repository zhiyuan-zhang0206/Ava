from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.host.net.tests.fake_url_opener import Answer, FakeUrlOpener
from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host
from scripts.ci.pull_requests import (
    accounting,
    commands,
    diagnose,
    job_rerun,
    owner_operations,
    status,
    trunk_api,
)


def _clock() -> Clock:
    timers = ManualTimers(wall_start=datetime(2026, 10, 1, tzinfo=UTC))
    return Clock(
        ClockConfig(timezone="UTC", authoritative_timezone="UTC", message_timestamp_weekday=False),
        timers=timers,
        now=timers.now,
    )


def _rollup(*, empty: bool = False) -> Reply:
    return Reply(
        stdout=json.dumps(
            {
                "mergeable": "MERGEABLE",
                "state": "OPEN",
                "headRefOid": "abc123",
                "statusCheckRollup": []
                if empty
                else [
                    {
                        "__typename": "CheckRun",
                        "name": "backend",
                        "workflowName": "CI",
                        "status": "COMPLETED",
                        "conclusion": "SUCCESS",
                    }
                ],
            }
        )
    )


@pytest.mark.parametrize("probe", ["rollup", "incomplete", "empty", "completed"])
def test_status_timeout_never_claims_green_or_no_checks(probe: str) -> None:
    runner = FakeCommands()
    error = subprocess.TimeoutExpired("gh", 30)
    runner.on(["gh", "pr", "view"], error if probe == "rollup" else _rollup(empty=probe == "empty"))
    runner.on(
        ["gh", "api", "repos/o/r/actions/runs?head_sha=abc123"],
        Reply(stdout="[]") if probe == "completed" else error,
    )
    runner.on(
        ["gh", "api", "repos/o/r/actions/runs?head_sha=abc123&status=completed&per_page=100"], error
    )
    result = status.check_ci("42", repo="o/r", host=fake_host(runner), clock=_clock())
    assert result.verdict is status.CIStatus.ERROR
    assert result.error_detail


def test_wait_reports_three_command_timeouts_as_query_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FakeCommands()
    runner.on(["gh", "pr", "view"], subprocess.TimeoutExpired("gh", 30), repeat=True)
    result = commands.main(
        ["42", "--repo", "o/r", "--wait", "--every", "1"],
        host=fake_host(runner),
        clock=_clock(),
        open_url=FakeUrlOpener(),
    )
    assert result == 3
    assert len(runner.calls) == status.MAX_CONSECUTIVE_ERRORS
    assert "timed out" in capsys.readouterr().err


@pytest.mark.parametrize("probe", ["runs", "jobs"])
def test_limbo_timeout_remains_missing_evidence(probe: str) -> None:
    runner = FakeCommands()
    error = subprocess.TimeoutExpired("gh", 30)
    candidates = Reply(
        stdout=json.dumps([{"id": 91, "name": "CI", "created_at": "2026-09-01T00:00:00Z"}])
    )
    runner.on(
        ["gh", "api", "repos/o/r/actions/runs?head_sha=abc123"],
        error if probe == "runs" else candidates,
    )
    runner.on(["gh", "api", "repos/o/r/actions/runs/91/jobs"], error)
    result = status.limbo_runs("abc123", "o/r", host=fake_host(runner), clock=_clock())
    assert result is None if probe == "runs" else result == []


@pytest.mark.parametrize("probe", ["gh", "git"])
def test_freshness_timeout_still_submits_the_green_pr(probe: str) -> None:
    # meiva:ref scripts/ci/pull_requests/docs/command-timeouts.meiva.okf.md#queue-fixture-token
    runner = FakeCommands()
    error = subprocess.TimeoutExpired(probe, 30)
    runner.on(["gh", "api", "repos/o/r/issues/42/timeline"], Reply(stdout="null"))
    runner.on(["gh", "api", "repos/o/r/pulls/42/commits"], Reply(stdout="null"))
    runner.on(["gh", "pr", "view"], _rollup(), error if probe == "gh" else Reply(stdout="a" * 40))
    runner.on(["gh", "api", "repos/o/r/actions/runs?head_sha=abc123"], Reply(stdout="[]"))
    runner.on(
        ["gh", "api", "repos/o/r/actions/runs?head_sha=abc123&status=completed&per_page=100"],
        Reply(stdout="1"),
    )
    runner.on("git", error)
    opener = FakeUrlOpener()
    opener.on("https://api.trunk.io/v1/submitPullRequest", Answer("{}"))
    opener.on("https://api.trunk.io/v1/getSubmittedPullRequest", Answer('{"state":"merged"}'))
    result = owner_operations.trunk_merge_flow(
        "42",
        "o/r",
        "medium",
        every=1,
        timeout=100,
        token="fixture",  # noqa: S106
        require_fresh_base=True,
        host=fake_host(runner),
        clock=_clock(),
        open_url=opener,
    )
    assert result == 0
    assert opener.urls == [
        "https://api.trunk.io/v1/submitPullRequest",
        "https://api.trunk.io/v1/getSubmittedPullRequest",
    ]


@pytest.mark.parametrize("probe", ["head", "runs", "jobs"])
def test_rerun_query_timeout_retains_its_cause(probe: str) -> None:
    runner = FakeCommands()
    error = subprocess.TimeoutExpired("gh", 30)
    runner.on(["gh", "pr", "view"], error if probe == "head" else Reply(stdout="abc123"))
    runner.on(
        ["gh", "api", "repos/o/r/actions/runs?head_sha=abc123&per_page=100"],
        error
        if probe == "runs"
        else Reply(
            stdout=json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 1,
                            "name": "CI",
                            "created_at": "2026-10-01T00:00:00Z",
                            "status": "completed",
                        }
                    ]
                }
            )
        ),
    )
    runner.on(["gh", "api", "repos/o/r/actions/runs/1/jobs?per_page=100"], error)
    with pytest.raises(job_rerun.CiJobRerunError, match="timed out") as caught:
        job_rerun.list_failed_jobs("42", "o/r", host=fake_host(runner))
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("count", [1, 2])
def test_rerun_post_timeout_reports_unconfirmed_outcome(
    count: int, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = FakeCommands()
    runner.on(["gh", "pr", "view"], Reply(stdout="abc123"))
    runner.on(
        ["gh", "api", "repos/o/r/actions/runs?head_sha=abc123&per_page=100"],
        Reply(
            stdout=json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 1,
                            "name": "CI",
                            "created_at": "2026-10-01T00:00:00Z",
                            "status": "completed",
                        }
                    ]
                }
            )
        ),
    )
    runner.on(
        ["gh", "api", "repos/o/r/actions/runs/1/jobs?per_page=100"],
        Reply(
            stdout=json.dumps(
                {
                    "jobs": [
                        {
                            "id": i,
                            "name": f"check {i}",
                            "status": "completed",
                            "conclusion": "failure",
                        }
                        for i in range(1, count + 1)
                    ]
                }
            )
        ),
    )
    runner.on(["gh", "api", "--method", "POST"], subprocess.TimeoutExpired("gh", 30))
    result = commands.main(
        ["42", "--repo", "o/r", "--queue", "trunk", "--rerun-failed-jobs"],
        host=fake_host(runner),
        clock=_clock(),
        open_url=FakeUrlOpener(),
    )
    assert result == 3
    out = capsys.readouterr().out
    assert "outcome unconfirmed" in out
    assert "Re-ran" not in out
    assert len([argv for argv in runner.argvs if "POST" in argv]) == 1


def test_rerun_query_timeout_is_reported_by_cli(capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeCommands()
    runner.on(["gh", "pr", "view"], subprocess.TimeoutExpired("gh", 30))
    result = commands.main(
        ["42", "--repo", "o/r", "--queue", "trunk", "--rerun-failed-jobs"],
        host=fake_host(runner),
        clock=_clock(),
        open_url=FakeUrlOpener(),
    )
    assert result == 1
    assert "Failed to list failed jobs" in capsys.readouterr().err


def test_best_effort_reads_keep_their_timeout_outcomes() -> None:
    runner = FakeCommands()
    runner.on("gh", subprocess.TimeoutExpired("gh", 60), repeat=True)
    host = fake_host(runner)
    with pytest.raises(accounting.CiUsageReadError, match="timed out"):
        accounting.collect("o/r", "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", host=host)
    assert owner_operations.last_head_update("42", "o/r", host=host) is None
    result = diagnose.diagnose_pr("42", "o/r", token=None, host=host, open_url=FakeUrlOpener())
    assert result["gh_error"] == "gh pr view failed"


@pytest.mark.parametrize(
    "operation",
    [
        "status",
        "runs",
        "completed",
        "limbo",
        "freshness",
        "rerun",
        "accounting",
        "diagnose",
        "cooldown",
    ],
)
def test_unexpected_command_error_is_never_a_missing_evidence_default(operation: str) -> None:
    runner = FakeCommands()
    error = RuntimeError("invalid command implementation")
    runner.on("gh", error)
    host = fake_host(runner)
    with pytest.raises(RuntimeError) as caught:
        if operation == "status":
            status.check_ci("42", repo="o/r", host=host, clock=_clock())
        elif operation == "runs":
            status.runs_not_yet_reporting("abc123", "o/r", host=host)
        elif operation == "completed":
            status.main_workflow_run_completed("abc123", "o/r", host=host)
        elif operation == "limbo":
            status.limbo_runs("abc123", "o/r", host=host, clock=_clock())
        elif operation == "freshness":
            trunk_api.base_freshness("42", "o/r", host=host)
        elif operation == "rerun":
            job_rerun.list_failed_jobs("42", "o/r", host=host)
        elif operation == "accounting":
            accounting.collect("o/r", "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", host=host)
        elif operation == "diagnose":
            diagnose.diagnose_pr("42", "o/r", token=None, host=host, open_url=FakeUrlOpener())
        else:
            owner_operations.last_head_update("42", "o/r", host=host)
    assert caught.value is error
