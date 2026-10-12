"""Workflow routing and actual recording commands preserve CI's test selection."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "refresh-test-durations.yml"


def _workflow(path: Path = _WORKFLOW) -> dict[object, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast("dict[object, Any]", document)


def test_schedule_backstop_and_dispatch_are_the_only_measurement_routes() -> None:
    workflow = _workflow()
    assert workflow[True]["schedule"] == [
        {"cron": "30 19 * * *"},
        {"cron": "30 21 * * *"},
    ]
    # Main runs no CI, so there is no completed main run to borrow timings from.
    assert set(workflow[True]) == {"schedule", "workflow_dispatch"}
    jobs = workflow["jobs"]
    assert jobs["check-refresh"]["if"] == "github.ref == 'refs/heads/main'"
    for name in ("measure-backend", "measure-e2e"):
        assert jobs[name]["if"] == "needs.check-refresh.outputs.mode == 'measure'"
    refresh = jobs["refresh"]
    assert "needs.check-refresh.outputs.mode == 'measure'" in refresh["if"]
    assert "reuse" not in refresh["if"]
    assert "needs.measure-backend.result == 'success'" in refresh["if"]
    assert "needs.measure-e2e.result == 'success'" in refresh["if"]


def test_refresh_pins_one_source_and_stamps_only_after_complete_merge() -> None:
    jobs = _workflow()["jobs"]
    refresh = jobs["refresh"]
    for name in ("measure-backend", "measure-e2e", "refresh"):
        checkout = jobs[name]["steps"][0]
        assert checkout["with"]["ref"] == "${{ needs.check-refresh.outputs.source-sha }}"
    steps = refresh["steps"]
    download = next(step for step in steps if step.get("name") == "Download shard measurements")
    assert download["with"]["run-id"] == "${{ needs.check-refresh.outputs.source-run-id }}"
    assert download["with"]["path"] == "${{ runner.temp }}/durations"
    merge_index = next(
        i for i, step in enumerate(steps) if step.get("name") == "Merge complete shard measurements"
    )
    stamp_index = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Record complete measurement provenance"
    )
    push_index = next(i for i, step in enumerate(steps) if step.get("id") == "publish")
    assert merge_index < stamp_index < push_index


def _recording_step(suite: str) -> dict[str, Any]:
    ci = _workflow(_REPO_ROOT / ".github/workflows/ci.yml")
    name = "Run pytest shard" if suite == "backend" else "Run e2e tests"
    return next(step for step in ci["jobs"][f"{suite}-shard"]["steps"] if step.get("name") == name)


@pytest.mark.parametrize("suite", ["backend", "e2e"])
def test_ci_shards_never_record_or_upload_durations(suite: str) -> None:
    """Timings come from the isolated nightly measurement, never from a CI run."""
    step = _recording_step(suite)
    assert "RECORD_DURATIONS" not in str(step)
    assert "--store-durations" not in step["run"]
    assert "--clean-durations" not in step["run"]
    ci = _workflow(_REPO_ROOT / ".github/workflows/ci.yml")
    names = [str(item.get("name")) for item in ci["jobs"][f"{suite}-shard"]["steps"]]
    assert not [name for name in names if "duration measurement" in name]


def test_publisher_rebuilds_old_bot_branch_from_measured_generation(tmp_path: Path) -> None:
    """Run the shipped candidate/stage/push steps against a real local remote."""
    remote = tmp_path / "remote.git"
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 — fixed git argv and local test-owned repositories
            ["git", *args],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "--bare", str(remote))
    git("init", "-b", "main")
    git("config", "user.name", "Duration test")
    git("config", "user.email", "duration@example.com")
    (checkout / "code-generation").write_text("old")
    (checkout / ".test_durations").write_text('{"old::test":1}')
    git("add", ".")
    git("commit", "-m", "old bot generation")
    git("remote", "add", "origin", str(remote))
    git("push", "origin", "HEAD:refs/heads/ava-bot/test-durations")
    (checkout / "code-generation").write_text("measured")
    git("add", "code-generation")
    git("commit", "-m", "measured main generation")
    source = git("rev-parse", "HEAD")
    git("push", "origin", "main")
    (checkout / ".test_durations").write_text('{"new::test":0.019}')
    provenance = {
        "source_sha": source,
        "run_id": 42,
        "measured_at": "2026-10-08T09:00:00Z",
        "schema_version": 1,
    }
    (checkout / ".test_durations.source.json").write_text(json.dumps(provenance))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text("#!/bin/sh\nprintf '1\\n'\n")
    fake_gh.chmod(0o755)
    candidate = tmp_path / "durations-candidate"
    steps = _workflow()["jobs"]["refresh"]["steps"]
    selectors = (
        ("name", "Prepare an isolated candidate branch"),
        ("name", "Stage the refreshed durations"),
        ("id", "publish"),
    )
    for key, value in selectors:
        step = next(step for step in steps if step.get(key) == value)
        result = subprocess.run(  # noqa: S603 — shipped publisher shell with a local remote
            ["bash", "-e", "-c", step["run"]],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=False,
            env=os.environ
            | {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "SOURCE_SHA": source,
                "RUNNER_TEMP": str(tmp_path),
                "CANDIDATE": str(candidate),
                "BRANCH": "ava-bot/test-durations",
                "GITHUB_OUTPUT": str(tmp_path / "output"),
                "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
    head = git("--git-dir", str(remote), "rev-parse", "refs/heads/ava-bot/test-durations")
    outputs = dict(line.split("=", 1) for line in (tmp_path / "output").read_text().splitlines())
    assert outputs["changed"] == "true"
    assert outputs["head-sha"] == head
    assert git("rev-parse", f"{head}^") == source
    assert git("show", f"{head}:code-generation") == "measured"
    assert json.loads(git("show", f"{head}:.test_durations")) == {"new::test": 0.019}
    assert json.loads(git("show", f"{head}:.test_durations.source.json")) == provenance
