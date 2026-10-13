"""The daily timing refresh saves an Actions cache; CI resolves and restores it consistently."""

import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "refresh-test-durations.yml"
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_RESOLVE = _REPO_ROOT / ".github" / "actions" / "resolve-test-durations" / "action.yml"
_RESTORE = _REPO_ROOT / ".github" / "actions" / "restore-test-durations" / "action.yml"
_NEWEST = "test-durations-2026-10-13-7-1"


def _yaml(path: Path) -> dict[object, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast("dict[object, Any]", document)


def _step(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [step for step in steps if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


def test_only_a_daily_schedule_and_dispatch_refresh_the_timings() -> None:
    workflow = _yaml(_WORKFLOW)
    assert workflow[True]["schedule"] == [{"cron": "30 19 * * *"}]
    assert set(workflow[True]) == {"schedule", "workflow_dispatch"}
    # Reads the cache listing and saves a cache: no write scope, no pull request, no secret.
    assert workflow["permissions"] == {"actions": "read", "contents": "read"}
    assert "secrets." not in _WORKFLOW.read_text(encoding="utf-8")
    jobs = workflow["jobs"]
    assert set(jobs) == {"plan", "measure-backend", "measure-e2e", "publish", "verify"}
    assert jobs["plan"]["if"] == "github.ref == 'refs/heads/main'"
    for name in ("measure-backend", "measure-e2e"):
        assert jobs[name]["needs"] == "plan"


def test_measured_shards_split_with_the_model_resolved_once_by_plan() -> None:
    jobs = _yaml(_WORKFLOW)["jobs"]
    assert jobs["plan"]["outputs"]["key"] == "${{ steps.durations.outputs.key }}"
    for name in ("measure-backend", "measure-e2e"):
        restore = _step(jobs[name]["steps"], "Restore the test durations")
        assert restore["uses"] == "./.github/actions/restore-test-durations"
        assert restore["with"]["key"] == "${{ needs.plan.outputs.key }}"


def test_the_cache_is_saved_only_after_a_complete_merge_under_a_dated_unique_key() -> None:
    jobs = _yaml(_WORKFLOW)["jobs"]
    publish = jobs["publish"]
    assert publish["needs"] == ["plan", "measure-backend", "measure-e2e"]
    assert "needs.measure-backend.result == 'success'" in publish["if"]
    assert "needs.measure-e2e.result == 'success'" in publish["if"]
    steps = publish["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Merge complete shard measurements") < names.index("Save .test_durations")
    save = _step(steps, "Save .test_durations")
    assert save["uses"].startswith("actions/cache/save@")
    assert save["with"] == {"path": ".test_durations", "key": "${{ steps.stamp.outputs.key }}"}
    stamp = _step(steps, "Name the cache entry")["run"]
    # Dated, and unique per run so a same-day dispatch cannot collide with an immutable entry.
    assert "date -u +%Y-%m-%d)-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in stamp
    assert stamp.startswith('echo "key=test-durations-')


def test_the_saved_cache_is_read_back_through_the_actions_ci_uses() -> None:
    verify = _yaml(_WORKFLOW)["jobs"]["verify"]
    assert verify["needs"] == "publish"
    steps = verify["steps"]
    assert _step(steps, "Resolve the test durations")["uses"] == (
        "./.github/actions/resolve-test-durations"
    )
    assert _step(steps, "Restore the test durations")["uses"] == (
        "./.github/actions/restore-test-durations"
    )
    check = _step(steps, "Check what was restored")
    assert check["env"]["SAVED_KEY"] == "${{ needs.publish.outputs.key }}"


def test_ci_resolves_once_in_classify_and_every_shard_restores_that_key() -> None:
    jobs = _yaml(_CI)["jobs"]
    classify = jobs["classify"]
    assert classify["permissions"] == {"contents": "read", "actions": "read"}
    assert classify["outputs"]["durations_key"] == "${{ steps.durations.outputs.key }}"
    resolve = _step(classify["steps"], "Resolve the test durations")
    assert resolve["id"] == "durations"
    assert resolve["uses"] == "./.github/actions/resolve-test-durations"
    names = [step.get("name") for step in classify["steps"]]
    # The selector reads the restored file, so the restore comes first.
    assert names.index("Restore the test durations") < names.index(
        "Select runtime-impact test subset"
    )
    for name in ("backend-shard", "e2e-shard"):
        job = jobs[name]
        restore = _step(job["steps"], "Restore the test durations")
        assert restore["uses"] == "./.github/actions/restore-test-durations"
        assert restore["with"]["key"] == "${{ needs.classify.outputs.durations_key }}"
        assert "classify" in job["needs"]


def test_a_key_that_stops_restoring_fails_instead_of_splitting_differently() -> None:
    steps = _yaml(_RESTORE)["runs"]["steps"]
    restore = _step(steps, "Restore .test_durations")
    assert restore["if"] == "inputs.key != ''"
    assert restore["with"]["fail-on-cache-miss"] is True
    assert restore["with"]["key"] == "${{ inputs.key }}"
    assert "restore-keys" not in restore["with"]
    note = _step(steps, "Note the missing durations")
    assert note["if"] == "inputs.key == ''"
    assert "::warning title=test durations::" in note["run"]


def _resolve(tmp_path: Path, gh_script: str) -> tuple[str, str]:
    """Run the resolve action's shell with a stand-in `gh`; return (key output, stdout)."""
    fake = tmp_path / "bin"
    fake.mkdir()
    gh = fake / "gh"
    gh.write_text(gh_script)
    gh.chmod(0o755)
    sleep = fake / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    script = _step(_yaml(_RESOLVE)["runs"]["steps"], "Find the newest test-durations cache")["run"]
    result = subprocess.run(  # noqa: S603 -- the checked-in step against a stand-in gh
        ["bash", "-eo", "pipefail", "-c", script],
        env=os.environ
        | {
            "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    return outputs["key"], result.stdout


def test_resolve_names_the_newest_main_cache(tmp_path: Path) -> None:
    resolved, stdout = _resolve(
        tmp_path,
        "#!/bin/sh\n"
        'case "$2" in *"ref=refs/heads/main"*"key=test-durations-"*"sort=created_at"*'
        f'"direction=desc"*) echo "{_NEWEST}";; *) exit 9;; esac\n',
    )
    assert resolved == _NEWEST
    assert "::warning" not in stdout


def test_resolve_with_no_cache_warns_and_returns_an_empty_key(tmp_path: Path) -> None:
    resolved, stdout = _resolve(tmp_path, "#!/bin/sh\necho ''\n")
    assert resolved == ""
    assert "::warning title=test durations::no test-durations cache exists on main" in stdout


def test_resolve_retries_then_degrades_with_a_warning_when_the_listing_fails(
    tmp_path: Path,
) -> None:
    resolved, stdout = _resolve(tmp_path, "#!/bin/sh\nexit 1\n")
    assert resolved == ""
    assert "::warning title=test durations::the cache listing could not be read" in stdout


@pytest.mark.parametrize("suite", ["backend", "e2e"])
def test_ci_shards_never_record_or_upload_durations(suite: str) -> None:
    """Timings come from the isolated daily measurement, never from a CI run."""
    jobs = _yaml(_CI)["jobs"]
    steps = jobs[f"{suite}-shard"]["steps"]
    name = "Run pytest shard" if suite == "backend" else "Run e2e tests"
    run = _step(steps, name)
    assert "RECORD_DURATIONS" not in str(run)
    assert "--store-durations" not in run["run"]
    assert "--clean-durations" not in run["run"]
    assert not [step for step in steps if "duration measurement" in str(step.get("name"))]
