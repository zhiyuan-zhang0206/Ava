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


def test_schedule_backstop_and_trusted_ci_have_separate_measurement_routes() -> None:
    workflow = _workflow()
    assert workflow[True]["schedule"] == [
        {"cron": "30 19 * * *"},
        {"cron": "30 21 * * *"},
    ]
    assert workflow[True]["workflow_run"] == {
        "workflows": ["CI"],
        "types": ["completed"],
        "branches": ["main"],
    }
    jobs = workflow["jobs"]
    guard = jobs["check-refresh"]
    for requirement in (
        "workflow_run.event == 'push'",
        "workflow_run.conclusion == 'success'",
        "workflow_run.head_branch == 'main'",
        "workflow_run.head_repository.full_name == github.repository",
    ):
        assert requirement in guard["if"]
    for name in ("measure-backend", "measure-e2e"):
        assert jobs[name]["if"] == "needs.check-refresh.outputs.mode == 'measure'"
    refresh = jobs["refresh"]
    assert "needs.check-refresh.outputs.mode == 'reuse'" in refresh["if"]
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
    push_index = next(
        i for i, step in enumerate(steps) if step.get("name") == "Push a review branch"
    )
    assert merge_index < stamp_index < push_index


def _recording_step(suite: str) -> dict[str, Any]:
    ci = _workflow(_REPO_ROOT / ".github/workflows/ci.yml")
    name = "Run pytest shard" if suite == "backend" else "Run e2e tests"
    return next(step for step in ci["jobs"][f"{suite}-shard"]["steps"] if step.get("name") == name)


@pytest.mark.parametrize(
    ("suite", "record", "group", "fail_first"),
    [
        ("backend", True, 1, True),
        ("backend", False, 1, True),
        ("backend", True, 8, True),
        ("e2e", True, 2, False),
        ("e2e", False, 2, False),
    ],
)
def test_existing_ci_command_records_main_only_and_reseeds_before_retry(
    tmp_path: Path,
    suite: str,
    record: bool,
    group: int,
    fail_first: bool,
) -> None:
    """Execute the shipped shell; a fake pytest corrupts attempt 1's timing input."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("""#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path
args = sys.argv[1:]
log = Path('calls.jsonl')
calls = len(log.read_text().splitlines()) if log.exists() else 0
measurement = None
if '--durations-path' in args:
    path = Path(args[args.index('--durations-path') + 1])
    measurement = json.loads(path.read_text())
    path.write_text('{"partial::timing":999}')
with log.open('a') as output:
    output.write(json.dumps({'args': args, 'seed': measurement}) + '\\n')
sys.exit(1 if os.environ['FAIL_FIRST'] == 'true' and calls == 0 else 0)
""")
    fake_uv.chmod(0o755)
    seed = {"existing::timing": 3.0}
    (tmp_path / ".test_durations").write_text(json.dumps(seed))
    step = _recording_step(suite)
    assert (
        step["env"]["RECORD_DURATIONS"]
        == "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}"
    )
    script = step["run"].replace("${{ matrix.group }}", str(group))
    result = subprocess.run(  # noqa: S603 — execute the repository's recording step with fake pytest
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ
        | {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "RECORD_DURATIONS": str(record).lower(),
            "FAIL_FIRST": str(fail_first).lower(),
            "GITHUB_OUTPUT": str(tmp_path / "output"),
            "COVERAGE_FILE": "coverage-data",
        },
    )
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert result.returncode == (1 if group == 8 else 0), result.stderr
    assert len(calls) == (2 if suite == "backend" and group != 8 else 1)
    _assert_calls(calls, suite=suite, record=record, group=group, seed=seed)
    assert json.loads((tmp_path / ".test_durations").read_text()) == seed


def _assert_calls(
    calls: list[dict[str, Any]],
    *,
    suite: str,
    record: bool,
    group: int,
    seed: dict[str, float],
) -> None:
    for call in calls:
        assert call["seed"] == (seed if record else None)
        args = call["args"]
        assert args[args.index("--group") + 1] == str(group)
        assert args[args.index("--splits") + 1] == ("16" if suite == "backend" else "4")
        assert args[args.index("-n") + 1] == ("4" if suite == "backend" else "2")
        assert args[args.index("--splitting-algorithm") + 1] == "least_duration"
        assert ("--store-durations" in args) is record
        assert ("--clean-durations" in args) is record
        if suite == "backend":
            assert "--omit-static-tests" in args
            assert "--ignore=tests/e2e" in args
            assert args[args.index("-m") + 1] == "not flaky"


def test_main_duration_upload_failure_cannot_change_required_ci_gates() -> None:
    ci = _workflow(_REPO_ROOT / ".github/workflows/ci.yml")
    for suite in ("backend", "e2e"):
        step = next(
            step
            for step in ci["jobs"][f"{suite}-shard"]["steps"]
            if step.get("name") == f"Upload {suite} duration measurement"
        )
        assert step["continue-on-error"] is True
        assert "success()" in step["if"]
        assert "github.event_name == 'push'" in step["if"]
        assert "github.ref == 'refs/heads/main'" in step["if"]


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
    names = (
        "Prepare an isolated candidate branch",
        "Stage the refreshed durations",
        "Push a review branch",
    )
    for name in names:
        step = next(step for step in steps if step.get("name") == name)
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
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
    head = git("--git-dir", str(remote), "rev-parse", "refs/heads/ava-bot/test-durations")
    assert git("rev-parse", f"{head}^") == source
    assert git("show", f"{head}:code-generation") == "measured"
    assert json.loads(git("show", f"{head}:.test_durations")) == {"new::test": 0.019}
    assert json.loads(git("show", f"{head}:.test_durations.source.json")) == provenance
