"""Contract tests for the test-duration refresh workflow backstop."""

import os
import subprocess
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


def test_recent_success_guard_fails_open_when_run_query_fails(tmp_path: Path) -> None:
    """The backstop must refresh rather than silently skip on a GitHub API outage."""
    guard_script = _load_workflow()["jobs"]["check-recent-success"]["steps"][0]["run"]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    fake_date = fake_bin / "date"
    fake_date.write_text("#!/bin/sh\nprintf '%s\\n' '2026-09-01T16:30:00Z'\n", encoding="utf-8")
    fake_date.chmod(0o755)

    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'simulated API outage' >&2\nexit 1\n", encoding="utf-8"
    )
    fake_gh.chmod(0o755)

    output = tmp_path / "github-output"
    environment = os.environ | {
        "GITHUB_OUTPUT": str(output),
        "GITHUB_REPOSITORY": "ava/example",
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    result = subprocess.run(  # noqa: S603 — fixed argv executes the repository-owned guard step
        ["bash", "-e", "-c", guard_script],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (
        "Unable to query successful duration refreshes; proceeding with measurements."
        in result.stdout
    )
    assert output.read_text(encoding="utf-8") == "should-refresh=true\n"
