"""Submit the publisher's exact generated duration PR to normal Trunk readiness."""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from base.host import proc
from scripts.ci.pull_requests import trunk_api


class _File(BaseModel):
    path: Literal[".test_durations", ".test_durations.source.json"]


class _Author(BaseModel):
    login: Literal["app/github-actions"]


class _Candidate(BaseModel):
    model_config = ConfigDict(strict=True)

    author: _Author
    base_ref: Literal["main"] = Field(alias="baseRefName")
    head_ref: Literal["ava-bot/test-durations"] = Field(alias="headRefName")
    head_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", alias="headRefOid")]
    is_cross_repository: Literal[False] = Field(alias="isCrossRepository")
    is_draft: Literal[False] = Field(alias="isDraft")
    state: Literal["OPEN"]
    files: list[_File]


def submit(pr: int, repository: str, head_sha: str, *, token: str) -> int:
    """Validate the just-published PR, then submit without overriding readiness."""
    if not token:
        raise ValueError("TRUNK_API_TOKEN is required for duration PR submission")
    result = proc.run_bounded(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            repository,
            "--json",
            "author,baseRefName,headRefName,headRefOid,isCrossRepository,isDraft,state,files",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    result.check_returncode()
    candidate = _Candidate.model_validate_json(result.stdout)
    if candidate.head_sha != head_sha:
        raise ValueError("duration PR head changed after publication; refusing submission")
    if not candidate.files or len({file.path for file in candidate.files}) != len(candidate.files):
        raise ValueError("duration PR must have a nonempty diff of generated duration files")
    _, error = trunk_api.post("submitPullRequest", trunk_api.pr_payload(str(pr), repository), token)
    if error == "HTTP 409":
        print(f"Duration PR #{pr} is already submitted to Trunk")
        return 0
    if error is not None:
        print(f"Duration PR #{pr} Trunk submission failed: {error}", file=sys.stderr)
        return 1
    print(f"Duration PR #{pr} submitted to Trunk; required checks still gate merging")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Read the API token from stdin so it never becomes a command-line argument."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)
    if args.pr <= 0:
        parser.error("--pr must be positive")
    try:
        return submit(args.pr, args.repository, args.head_sha, token=sys.stdin.read().strip())
    except (ValueError, subprocess.SubprocessError) as error:
        print(f"Duration PR submission refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
