"""Contract tests for the test-duration refresh workflow backstop."""

from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "refresh-test-durations.yml"


def _load_workflow() -> dict[object, Any]:
    document = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast("dict[object, Any]", document)


def test_schedule_has_two_hour_backstop() -> None:
    workflow = _load_workflow()
    # PyYAML follows YAML 1.1, where GitHub Actions' `on` key parses as true.
    schedules = workflow[True]["schedule"]

    assert schedules == [
        {"cron": "30 19 * * *"},
        {"cron": "30 21 * * *"},
    ]


def test_recent_success_guard_skips_all_measurements() -> None:
    jobs = _load_workflow()["jobs"]
    guard = jobs["check-recent-success"]
    guard_step = guard["steps"][0]
    guard_script = guard_step["run"]

    assert guard_step["env"]["GH_TOKEN"] == "${{ github.token }}"  # noqa: S105
    assert "date --utc --date='5 hours ago'" in guard_script
    assert "actions/workflows/refresh-test-durations.yml/runs" in guard_script
    assert "workflow_file=" not in guard_script
    assert "status=success" in guard_script
    assert "created=>=$CUTOFF" in guard_script
    assert "should-refresh=false" in guard_script
    assert "exit 0" in guard_script
    assert "should-refresh=true" in guard_script
    assert (
        guard["outputs"]["should-refresh"] == "${{ steps.recent-success.outputs.should-refresh }}"
    )

    for job_name in ("measure-backend", "measure-e2e", "refresh"):
        job = jobs[job_name]
        assert "check-recent-success" in job["needs"]
        assert "needs.check-recent-success.outputs.should-refresh == 'true'" in job["if"]
