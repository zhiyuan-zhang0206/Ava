"""Execute the real retry shell with a mock GitHub API; never call the network."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = "owner/repository"
SHA = "a" * 40
OTHER_SHA = "b" * 40


def run_retry(
    tmp_path: Path, **overrides: str
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci-rerun.yml").read_text())
    script = workflow["jobs"]["rerun-failed-jobs"]["steps"][0]["run"]
    mock = tmp_path / "gh"
    mock.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['CALL_LOG'], 'a') as log: log.write(json.dumps(args) + '\\n')\n"
        "if os.environ.get('API_FAIL') == '1': sys.exit(1)\n"
        "if '--method' in args: sys.exit(0)\n"
        "endpoint = args[1]\n"
        "if '/pulls/' in endpoint: print(os.environ['PR_RESPONSE'])\n"
        "elif '/commits/' in endpoint: print(os.environ['CURRENT_SHA'])\n"
        "else: print(os.environ['NEWEST_RUN'])\n"
    )
    mock.chmod(0o700)
    call_log = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.defpath}",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "RUN_ID": "100",
        "RUN_ATTEMPT": "1",
        "RUN_CONCLUSION": "failure",
        "RUN_EVENT": "pull_request",
        "HEAD_SHA": SHA,
        "HEAD_BRANCH": "feature",
        "HEAD_REPO": REPO,
        "PR_NUMBER": "42",
        "PR_RESPONSE": f"open\t{SHA}\t{REPO}\t{REPO}",
        "CURRENT_SHA": SHA,
        "NEWEST_RUN": "100",
        "CALL_LOG": str(call_log),
        "API_FAIL": "0",
        **overrides,
    }
    result = subprocess.run(  # noqa: S603 — checked-in shell; gh is an isolated mock
        ["/bin/bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    calls = (
        [json.loads(line) for line in call_log.read_text().splitlines()]
        if call_log.exists()
        else []
    )
    return result, calls


@pytest.mark.parametrize("event", ["pull_request", "push"])
def test_current_head_retries_once(tmp_path: Path, event: str) -> None:
    result, calls = run_retry(tmp_path, RUN_EVENT=event)
    assert result.returncode == 0, result.stderr
    posts = [call for call in calls if "POST" in call]
    assert posts == [
        ["api", "--method", "POST", f"repos/{REPO}/actions/runs/100/rerun-failed-jobs"]
    ]


def test_verified_fork_head_is_not_confused_with_base_repository(tmp_path: Path) -> None:
    fork = "contributor/repository"
    result, calls = run_retry(tmp_path, HEAD_REPO=fork, PR_RESPONSE=f"open\t{SHA}\t{REPO}\t{fork}")
    assert result.returncode == 0, result.stderr
    assert sum("POST" in call for call in calls) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"PR_RESPONSE": f"open\t{OTHER_SHA}\t{REPO}\t{REPO}"},
        {"PR_RESPONSE": f"closed\t{SHA}\t{REPO}\t{REPO}"},
        {"PR_RESPONSE": f"open\t{SHA}\tother/repo\t{REPO}"},
        {"PR_RESPONSE": f"open\t{SHA}\t{REPO}\tother/repo"},
        {"PR_NUMBER": ""},
        {"PR_NUMBER": "42/other"},
        {"RUN_ATTEMPT": "2"},
        {"RUN_CONCLUSION": "cancelled"},
        {"NEWEST_RUN": "101"},
        {"RUN_EVENT": "push", "CURRENT_SHA": OTHER_SHA},
        {"RUN_EVENT": "push", "HEAD_REPO": "other/repo"},
        {"RUN_EVENT": "workflow_dispatch"},
    ],
)
def test_stale_or_unverified_event_never_posts(tmp_path: Path, overrides: dict[str, str]) -> None:
    result, calls = run_retry(tmp_path, **overrides)
    assert result.returncode == 0, result.stderr
    assert not any("POST" in call for call in calls)


def test_api_failure_is_not_permission_to_retry(tmp_path: Path) -> None:
    result, calls = run_retry(tmp_path, API_FAIL="1")
    assert result.returncode != 0
    assert not any("POST" in call for call in calls)


def test_cross_sha_guard_race_cannot_share_native_concurrency_group() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    group = workflow["concurrency"]["group"]
    expression = "${{ github.sha }}"
    assert group == "ci-${{ github.ref }}-" + expression
    ref = "refs/pull/42/merge"
    old = group.replace("${{ github.ref }}", ref).replace(expression, SHA)
    new = group.replace("${{ github.ref }}", ref).replace(expression, OTHER_SHA)
    # Native Actions cancellation/replacement only applies inside one group.
    # This remains different if PR synchronization happens after the GET guard.
    assert old != new
    assert old == group.replace("${{ github.ref }}", ref).replace(expression, SHA)
