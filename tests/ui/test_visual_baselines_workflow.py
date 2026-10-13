"""The visual-baselines publish job approves the approval-held CI run its bot push spawns."""

import os
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "visual-baselines.yml"
_APPROVE_STEP = "Approve the pull_request CI run spawned by the bot push"
_HEAD_SHA = "1234567890abcdef1234567890abcdef12345678"
_RUN_ID = "424242"


def _yaml(path: Path) -> dict[object, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast("dict[object, Any]", document)


def _approve_step() -> dict[str, Any]:
    steps = _yaml(_WORKFLOW)["jobs"]["publish"]["steps"]
    matches = [step for step in steps if step.get("name") == _APPROVE_STEP]
    assert len(matches) == 1, f"expected exactly one step named {_APPROVE_STEP!r}"
    return cast("dict[str, Any]", matches[0])


def test_the_approve_step_stays_dispatch_only_and_fail_soft() -> None:
    step = _approve_step()
    assert step["if"] == "github.event_name == 'workflow_dispatch'"
    expected_env = "${{ github.token }}"
    assert step["env"]["GH_TOKEN"] == expected_env
    run = step["run"]
    # The explicit workflow_dispatch run below is the authoritative green
    # signal, so this best-effort approval must never fail the publish job.
    assert "exit 1" not in run
    # ...but it must not stay silent when it cannot approve either: the warning
    # names the situation so a maintainer can act.
    assert "::warning title=visual baselines::" in run


def test_the_approve_step_polls_for_the_held_run_of_the_pushed_commit() -> None:
    run = _approve_step()["run"]
    # The synchronized run appears a few seconds after the push. A single
    # "newest run on the branch" lookup reads the branch's previous run and its
    # approve call fails with "This workflow run is not waiting for approval"
    # (HTTP 403) - the failure observed on run 38430429569, 2026-10-13.
    assert 'HEAD_SHA="$(git rev-parse HEAD)"' in run
    assert '--commit "$HEAD_SHA"' in run
    assert "--status action_required" in run
    assert "actions/runs/$RUN_ID/approve" in run
    # Bounded polling: 18 attempts five seconds apart, never a single lookup.
    assert "for attempt in {1..18}" in run
    assert "sleep 5" in run


def _run_approve_step(tmp_path: Path, gh_script: str) -> subprocess.CompletedProcess[str]:
    """Run the checked-in approve step against a stand-in gh; return the result."""
    fake = tmp_path / "bin"
    fake.mkdir()
    gh = fake / "gh"
    gh.write_text(gh_script)
    gh.chmod(0o755)
    git = fake / "git"
    git.write_text(f"#!/bin/sh\nprintf '%s\\n' '{_HEAD_SHA}'\n")
    git.chmod(0o755)
    sleep = fake / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    return subprocess.run(  # noqa: S603 -- the checked-in step against a stand-in gh
        ["bash", "-eo", "pipefail", "-c", _approve_step()["run"]],
        env=os.environ
        | {
            "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "owner/repo",
            "BRANCH": "ava-test-branch",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_held_run_on_the_pushed_commit_is_approved(tmp_path: Path) -> None:
    calls = tmp_path / "gh-calls"
    result = _run_approve_step(
        tmp_path,
        f"""#!/bin/sh
case "$*" in
  *"run list"*"--commit {_HEAD_SHA}"*"--status action_required"*) echo {_RUN_ID} ;;
  *"api --method POST repos/owner/repo/actions/runs/{_RUN_ID}/approve"*) echo "$*" >> {calls} ;;
  *) exit 9 ;;
esac
""",
    )
    assert result.returncode == 0, result.stderr
    assert f"Approved pending CI run {_RUN_ID} for {_HEAD_SHA}" in result.stdout
    assert "::warning" not in result.stdout
    assert calls.read_text().splitlines() == [
        f"api --method POST repos/owner/repo/actions/runs/{_RUN_ID}/approve"
    ]


def test_the_step_waits_while_the_run_has_not_appeared_yet(tmp_path: Path) -> None:
    attempts = tmp_path / "attempts"
    result = _run_approve_step(
        tmp_path,
        f"""#!/bin/sh
case "$*" in
  *"run list"*)
    n=0
    if [ -f {attempts} ]; then n="$(cat {attempts})"; fi
    n=$((n + 1))
    echo "$n" > {attempts}
    if [ "$n" -ge 3 ]; then echo {_RUN_ID}; fi
    ;;
  *"api --method POST repos/owner/repo/actions/runs/{_RUN_ID}/approve"*) ;;
  *) exit 9 ;;
esac
""",
    )
    assert result.returncode == 0, result.stderr
    assert f"Approved pending CI run {_RUN_ID} for {_HEAD_SHA}" in result.stdout
    assert "::warning" not in result.stdout
    assert attempts.read_text().strip() == "3"


def test_no_held_run_times_out_fail_soft_with_an_explicit_warning(tmp_path: Path) -> None:
    polls = tmp_path / "polls"
    result = _run_approve_step(
        tmp_path,
        f"""#!/bin/sh
case "$*" in
  *"run list"*)
    n=0
    if [ -f {polls} ]; then n="$(cat {polls})"; fi
    echo "$((n + 1))" > {polls}
    ;;
  *) exit 9 ;;
esac
""",
    )
    assert result.returncode == 0  # fail-soft: a missing run must not fail the job
    assert (
        f"::warning title=visual baselines::no approval-held pull_request run appeared for {_HEAD_SHA}"
        in result.stdout
    )
    assert "Approved pending CI run" not in result.stdout
    # Bounded: exactly 18 attempts, then the warning - never an unbounded wait.
    assert polls.read_text().strip() == "18"


def test_a_run_that_cannot_be_approved_is_surfaced_by_id(tmp_path: Path) -> None:
    result = _run_approve_step(
        tmp_path,
        f"""#!/bin/sh
case "$*" in
  *"run list"*) echo {_RUN_ID} ;;
  *"api --method POST repos/owner/repo/actions/runs/{_RUN_ID}/approve"*) exit 1 ;;
  *) exit 9 ;;
esac
""",
    )
    assert result.returncode == 0
    assert (
        f"::warning title=visual baselines::run {_RUN_ID} for {_HEAD_SHA} stayed without approval"
        in result.stdout
    )
