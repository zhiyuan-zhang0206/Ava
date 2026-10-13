"""The visual-baselines publish job approves the approval-held CI runs its bot push spawns."""

import os
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "visual-baselines.yml"
_APPROVE_STEP = "Approve the pull_request CI runs spawned by the bot push"
_HEAD_SHA = "1234567890abcdef1234567890abcdef12345678"


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


def test_the_approve_step_polls_for_the_held_runs_of_the_pushed_commit() -> None:
    run = _approve_step()["run"]
    # The held runs appear a few seconds after the push, so a single immediate
    # lookup read the branch's previous run and its approve call failed with
    # "This workflow run is not waiting for approval" (HTTP 403) - the failure
    # observed on run 38430429569, 2026-10-13.
    assert 'HEAD_SHA="$(git rev-parse HEAD)"' in run
    assert '--commit "$HEAD_SHA"' in run
    assert "--status action_required" in run
    # Approval covers every parked run of the pushed commit: the parked set
    # varies with the pull request's diff (ci.yml always; App CI and others by
    # their path filters), so no single-workflow filter is allowed here.
    assert "--workflow" not in run
    assert "actions/runs/$RUN_ID/approve" in run
    # Bounded polling: 18 attempts five seconds apart, three quiet attempts
    # after the last approval, never a single lookup.
    assert "for attempt in {1..18}" in run
    assert "sleep 5" in run
    assert "quiet=$((quiet + 1))" in run


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


def test_a_held_run_is_approved_once_and_the_step_settles(tmp_path: Path) -> None:
    calls = tmp_path / "gh-calls"
    polls = tmp_path / "polls"
    result = _run_approve_step(
        tmp_path,
        f"""#!/bin/sh
case "$*" in
  *"run list"*)
    echo "$(( $(cat {polls} 2>/dev/null || echo 0) + 1 ))" > {polls}
    echo 424242
    ;;
  *"api --method POST repos/owner/repo/actions/runs/424242/approve"*) echo "$*" >> {calls} ;;
  *) exit 9 ;;
esac
""",
    )
    assert result.returncode == 0, result.stderr
    assert "Approved pending CI run 424242" in result.stdout
    assert f"All pending CI runs for {_HEAD_SHA} are approved: 424242" in result.stdout
    assert "::warning" not in result.stdout
    assert calls.read_text().splitlines() == [
        "api --method POST repos/owner/repo/actions/runs/424242/approve"
    ]
    assert polls.read_text().strip() == "4"  # the approval, then three quiet attempts


def test_two_held_runs_on_the_same_commit_are_both_approved(tmp_path: Path) -> None:
    calls = tmp_path / "gh-calls"
    result = _run_approve_step(
        tmp_path,
        f"""#!/bin/sh
case "$*" in
  *"run list"*) printf '%s\n%s\n' 111222 333444 ;;
  *"api --method POST repos/owner/repo/actions/runs/111222/approve"*) echo "$*" >> {calls} ;;
  *"api --method POST repos/owner/repo/actions/runs/333444/approve"*) echo "$*" >> {calls} ;;
  *) exit 9 ;;
esac
""",
    )
    assert result.returncode == 0, result.stderr
    assert "Approved pending CI run 111222" in result.stdout
    assert "Approved pending CI run 333444" in result.stdout
    assert f"All pending CI runs for {_HEAD_SHA} are approved: 111222 333444" in result.stdout
    assert calls.read_text().splitlines() == [
        "api --method POST repos/owner/repo/actions/runs/111222/approve",
        "api --method POST repos/owner/repo/actions/runs/333444/approve",
    ]


def test_a_run_created_a_little_later_is_still_approved(tmp_path: Path) -> None:
    calls = tmp_path / "gh-calls"
    polls = tmp_path / "polls"
    result = _run_approve_step(
        tmp_path,
        f"""#!/bin/sh
case "$*" in
  *"run list"*)
    n=$(( $(cat {polls} 2>/dev/null || echo 0) + 1 )); echo "$n" > {polls}
    if [ "$n" -ge 3 ]; then echo 424242; fi
    ;;
  *"api --method POST repos/owner/repo/actions/runs/424242/approve"*) echo "$*" >> {calls} ;;
  *) exit 9 ;;
esac
""",
    )
    assert result.returncode == 0, result.stderr
    assert "Approved pending CI run 424242" in result.stdout
    assert polls.read_text().strip() == "6"  # two empty polls, the approval, three quiet ones
    assert calls.read_text().splitlines() == [
        "api --method POST repos/owner/repo/actions/runs/424242/approve"
    ]


def test_a_second_run_appearing_after_the_first_approval_is_approved(tmp_path: Path) -> None:
    calls = tmp_path / "gh-calls"
    polls = tmp_path / "polls"
    result = _run_approve_step(
        tmp_path,
        f"""#!/bin/sh
case "$*" in
  *"run list"*)
    n=$(( $(cat {polls} 2>/dev/null || echo 0) + 1 )); echo "$n" > {polls}
    if [ "$n" -ge 2 ]; then printf '%s\n%s\n' 111222 333444; else echo 111222; fi
    ;;
  *"api --method POST repos/owner/repo/actions/runs/111222/approve"*) echo "$*" >> {calls} ;;
  *"api --method POST repos/owner/repo/actions/runs/333444/approve"*) echo "$*" >> {calls} ;;
  *) exit 9 ;;
esac
""",
    )
    assert result.returncode == 0, result.stderr
    assert f"All pending CI runs for {_HEAD_SHA} are approved: 111222 333444" in result.stdout
    assert polls.read_text().strip() == "5"  # the first run, the straggler, three quiet ones
    assert calls.read_text().splitlines() == [
        "api --method POST repos/owner/repo/actions/runs/111222/approve",
        "api --method POST repos/owner/repo/actions/runs/333444/approve",
    ]


def test_an_approval_that_fails_once_is_retried(tmp_path: Path) -> None:
    calls = tmp_path / "gh-calls"
    attempts = tmp_path / "attempts"
    result = _run_approve_step(
        tmp_path,
        f"""#!/bin/sh
case "$*" in
  *"run list"*) echo 555 ;;
  *"api --method POST repos/owner/repo/actions/runs/555/approve"*)
    a=$(( $(cat {attempts} 2>/dev/null || echo 0) + 1 )); echo "$a" > {attempts}
    if [ "$a" -ge 2 ]; then echo "$*" >> {calls}; else exit 1; fi
    ;;
  *) exit 9 ;;
esac
""",
    )
    assert result.returncode == 0, result.stderr
    assert "Approval for pending run 555 failed; will retry" in result.stdout
    assert "Approved pending CI run 555" in result.stdout
    assert attempts.read_text().strip() == "2"
    assert calls.read_text().splitlines() == [
        "api --method POST repos/owner/repo/actions/runs/555/approve"
    ]


def test_a_run_that_stays_held_times_out_with_a_warning_naming_it(tmp_path: Path) -> None:
    calls = tmp_path / "gh-calls"
    result = _run_approve_step(
        tmp_path,
        f"""#!/bin/sh
case "$*" in
  *"run list"*) echo 777 ;;
  *"api --method POST repos/owner/repo/actions/runs/777/approve"*) echo "$*" >> {calls}; exit 1 ;;
  *) exit 9 ;;
esac
""",
    )
    assert result.returncode == 0  # fail-soft
    assert "::warning title=visual baselines::run(s) 777" in result.stdout
    assert "are still waiting for approval" in result.stdout
    assert len(calls.read_text().splitlines()) == 18  # one attempt per bounded poll


def test_no_held_run_times_out_fail_soft_with_an_explicit_warning(tmp_path: Path) -> None:
    polls = tmp_path / "polls"
    result = _run_approve_step(
        tmp_path,
        f"""#!/bin/sh
case "$*" in
  *"run list"*)
    n=$(( $(cat {polls} 2>/dev/null || echo 0) + 1 )); echo "$n" > {polls}
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
    # Bounded: 18 attempts, then the final check - never an unbounded wait.
    assert polls.read_text().strip() == "19"
