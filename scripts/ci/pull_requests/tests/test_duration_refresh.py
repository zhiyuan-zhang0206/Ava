"""Generated duration admission refuses altered PRs and preserves Trunk readiness."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from base.host import proc
from scripts.ci.pull_requests import duration_refresh, trunk_api

_HEAD = "a" * 40
_REPO = "zhiyuan-zhang0206/Ava"
type Submission = tuple[str, dict[str, object], str]


@pytest.fixture
def credential() -> str:
    return "test-token"


@pytest.fixture
def candidate() -> dict[str, Any]:
    return {
        "author": {"login": "app/github-actions"},
        "baseRefName": "main",
        "headRefName": "ava-bot/test-durations",
        "headRefOid": _HEAD,
        "isCrossRepository": False,
        "isDraft": False,
        "state": "OPEN",
        "files": [{"path": ".test_durations"}, {"path": ".test_durations.source.json"}],
    }


@pytest.fixture
def submissions(monkeypatch: pytest.MonkeyPatch, candidate: dict[str, Any]) -> list[Submission]:
    calls: list[Submission] = []

    def gh(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command[:6] == ["gh", "pr", "view", "5082", "--repo", _REPO]
        assert "check" not in kwargs
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(candidate))

    def post(
        endpoint: str, payload: dict[str, object], token: str
    ) -> tuple[dict[str, object], str | None]:
        calls.append((endpoint, payload, token))
        return {}, None

    monkeypatch.setattr(duration_refresh.proc, "run_bounded", gh)
    monkeypatch.setattr(trunk_api, "post", post)
    return calls


def test_generated_pr_submits_with_normal_readiness(
    submissions: list[Submission], capsys: pytest.CaptureFixture[str], credential: str
) -> None:
    assert duration_refresh.submit(5082, _REPO, _HEAD, token=credential) == 0
    assert submissions == [
        (
            "submitPullRequest",
            {
                "repo": {"host": "github.com", "owner": "zhiyuan-zhang0206", "name": "Ava"},
                "pr": {"number": 5082},
                "targetBranch": "main",
            },
            "test-token",
        )
    ]
    assert "required checks still gate" in capsys.readouterr().out


@pytest.mark.parametrize(
    "changed",
    [
        {"headRefOid": "b" * 40},
        {"files": [{"path": ".test_durations"}, {"path": "scripts/ci/test_selector.py"}]},
        {"files": []},
        {"files": [{"path": ".test_durations"}, {"path": ".test_durations"}]},
        {"author": {"login": "human"}},
        {"baseRefName": "release"},
        {"headRefName": "another-branch"},
        {"isCrossRepository": True},
        {"isDraft": True},
        {"state": "CLOSED"},
    ],
)
def test_altered_candidate_never_submits(
    candidate: dict[str, Any],
    submissions: list[Submission],
    changed: dict[str, Any],
    credential: str,
) -> None:
    candidate.update(changed)
    with pytest.raises(ValueError):
        duration_refresh.submit(5082, _REPO, _HEAD, token=credential)
    assert submissions == []


def test_missing_candidate_field_is_not_defaulted(
    candidate: dict[str, Any], submissions: list[Submission], credential: str
) -> None:
    del candidate["isDraft"]
    with pytest.raises(ValidationError):
        duration_refresh.submit(5082, _REPO, _HEAD, token=credential)
    assert submissions == []


def test_already_submitted_response_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    submissions: list[Submission],
    capsys: pytest.CaptureFixture[str],
    credential: str,
) -> None:
    def already_submitted(*_: object) -> tuple[None, str]:
        return None, "HTTP 409"

    monkeypatch.setattr(trunk_api, "post", already_submitted)
    assert duration_refresh.submit(5082, _REPO, _HEAD, token=credential) == 0
    assert "already submitted" in capsys.readouterr().out


def test_api_failure_fails_the_publisher(
    monkeypatch: pytest.MonkeyPatch,
    submissions: list[Submission],
    capsys: pytest.CaptureFixture[str],
    credential: str,
) -> None:
    def failed(*_: object) -> tuple[None, str]:
        return None, "HTTP 401"

    monkeypatch.setattr(trunk_api, "post", failed)
    assert duration_refresh.submit(5082, _REPO, _HEAD, token=credential) == 1
    assert "HTTP 401" in capsys.readouterr().err


def test_cli_reads_token_from_stdin_and_refuses_missing_token(
    monkeypatch: pytest.MonkeyPatch,
    submissions: list[Submission],
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = ["--repository", _REPO, "--pr", "5082", "--head-sha", _HEAD]
    monkeypatch.setattr(duration_refresh.sys, "stdin", io.StringIO(""))
    assert duration_refresh.main(args) == 1
    assert submissions == []
    assert "TRUNK_API_TOKEN is required" in capsys.readouterr().err
    monkeypatch.setattr(duration_refresh.sys, "stdin", io.StringIO("test-token"))
    assert duration_refresh.main(args) == 0
    assert submissions[0][2] == "test-token"


def git(candidate: Path, *args: str) -> str:
    result = proc.run_bounded(
        ["git", "-C", str(candidate), *args], timeout=30, capture_output=True, text=True
    )
    result.check_returncode()
    return result.stdout.strip()


@pytest.fixture
def publication_repo(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    git(candidate, "init")
    git(candidate, "config", "user.name", "Duration test")
    git(candidate, "config", "user.email", "duration@example.com")
    for name in (".test_durations", ".test_durations.source.json"):
        (candidate / name).write_text("{}\n")
    git(candidate, "add", ".")
    git(candidate, "commit", "-m", "measured source")
    return candidate


def run_publisher(candidate: Path, tmp_path: Path) -> dict[str, str]:
    workflow = Path(__file__).resolve().parents[4] / ".github/workflows/refresh-test-durations.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["refresh"]["steps"]
    publish = next(step for step in steps if step.get("id") == "publish")
    outputs, summary = tmp_path / "outputs", tmp_path / "summary"
    result = proc.run_bounded(
        ["bash", "-e", "-o", "pipefail", "-c", publish["run"]],
        env={
            **os.environ,
            "CANDIDATE": str(candidate),
            "BRANCH": "ava-bot/test-durations",
            "GITHUB_OUTPUT": str(outputs),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        timeout=30,
        capture_output=True,
        text=True,
    )
    result.check_returncode()
    return dict(line.split("=", 1) for line in outputs.read_text().splitlines())


def test_no_generated_diff_does_not_push_or_open_a_pr(
    publication_repo: Path, tmp_path: Path
) -> None:
    assert run_publisher(publication_repo, tmp_path) == {"changed": "false"}
    assert "nothing to publish" in (tmp_path / "summary").read_text()


def test_identical_published_snapshot_reuses_head_but_still_admits(
    publication_repo: Path, tmp_path: Path
) -> None:
    source = git(publication_repo, "rev-parse", "HEAD")
    for name in (".test_durations", ".test_durations.source.json"):
        (publication_repo / name).write_text('{"new": 1}\n')
    git(publication_repo, "add", ".")
    git(publication_repo, "commit", "-m", "previous publication")
    published = git(publication_repo, "rev-parse", "HEAD")
    git(publication_repo, "update-ref", "refs/remotes/origin/ava-bot/test-durations", published)
    git(publication_repo, "checkout", source)
    for name in (".test_durations", ".test_durations.source.json"):
        (publication_repo / name).write_text('{"new": 1}\n')
    assert run_publisher(publication_repo, tmp_path) == {"changed": "true", "head-sha": published}
    assert git(publication_repo, "rev-parse", "HEAD") == source
    assert "already published" in (tmp_path / "summary").read_text()


def test_unchanged_weights_with_new_provenance_can_submit(
    candidate: dict[str, Any], submissions: list[Submission], credential: str
) -> None:
    candidate["files"] = [{"path": ".test_durations.source.json"}]
    assert duration_refresh.submit(5082, _REPO, _HEAD, token=credential) == 0
    assert len(submissions) == 1


def test_admission_uses_workflow_revision_and_requires_publication() -> None:
    workflow = Path(__file__).resolve().parents[4] / ".github/workflows/refresh-test-durations.yml"
    jobs = yaml.safe_load(workflow.read_text())["jobs"]
    job = jobs["submit"]
    assert job["needs"] == "refresh"
    assert {part.strip() for part in job["if"].split("&&")} == {
        "!cancelled()",
        "needs.refresh.result == 'success'",
        "needs.refresh.outputs.changed == 'true'",
        "needs.refresh.outputs.pr-number != ''",
        "needs.refresh.outputs.head-sha != ''",
    }
    checkout = next(
        step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ github.workflow_sha }}"
    submit = job["steps"][-1]
    assert submit["env"]["CANDIDATE_SHA"] == "${{ needs.refresh.outputs.head-sha }}"
    assert submit["env"]["PR_NUMBER"] == "${{ needs.refresh.outputs.pr-number }}"
    pull_request = next(
        step for step in jobs["refresh"]["steps"] if step.get("id") == "pull-request"
    )
    assert pull_request["if"] == "steps.publish.outputs.changed == 'true'"
