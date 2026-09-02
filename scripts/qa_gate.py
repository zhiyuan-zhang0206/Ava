"""Publish exact-head QA status using trusted default-branch code only."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.qa_receipt import REPOSITORY, approved


def _api(path: str, *args: str) -> Any:
    result = subprocess.run(  # noqa: S603 — fixed gh argv, trusted repo, integer PR, no shell
        ["gh", "api", f"repos/{REPOSITORY}/{path}", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _records(path: str) -> list[dict[str, Any]]:
    return [record for page in _api(path, "--paginate", "--slurp") for record in page]


def evaluate(pr_number: int) -> None:
    pr = _api(f"pulls/{pr_number}")
    sha = pr["head"]["sha"]

    def status(state: str, description: str) -> None:
        _api(
            f"statuses/{sha}",
            "--method",
            "POST",
            "-f",
            f"state={state}",
            "-f",
            "context=qa-approved-gate",
            "-f",
            f"description={description}",
        )

    # Invalidate an earlier success before reads that may fail halfway.
    status("pending", "Rechecking exact-head QA evidence")
    comments = _records(f"issues/{pr_number}/comments?per_page=100")
    reviews = _records(f"pulls/{pr_number}/reviews?per_page=100")
    current = _api(f"pulls/{pr_number}")
    if current["head"]["sha"] != sha:
        status("failure", "HEAD changed while QA was evaluated")
        return
    # Commit statuses are SHA-global, not PR-bound. Do not let one PR's
    # receipt authorize another PR pointing at the same commit.
    same_head = [
        item
        for item in _records(f"commits/{sha}/pulls?per_page=100")
        if item["state"] == "open" and item["head"]["sha"] == sha
    ]
    if {item["number"] for item in same_head} != {pr_number}:
        status("failure", "Ambiguous shared HEAD: use a unique commit per open PR")
        return
    verdict = approved(current, comments, reviews)
    status(
        "success" if verdict else "failure",
        "Current HEAD has authorized QA evidence"
        if verdict
        else "Missing current-head QA approval",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pr", type=int)
    evaluate(parser.parse_args().pr)
