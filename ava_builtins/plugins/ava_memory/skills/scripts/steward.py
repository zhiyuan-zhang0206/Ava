"""Per-machine steward: stage, commit, push, create PR.

Usage: python3 steward.py -m "memory: <machine> <date> local sync"

The multi-host flow: each machine runs this on its own checkout, creating a
PR from machine-<name> → main. It never merges — that is the arbiter's job.
After it returns, message the Memory Arbiter that the PR is ready.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import branch_name, pool_dir, repo_slug, run, stage_and_commit


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-machine memory steward sync")
    ap.add_argument("-m", "--message", required=True, help="commit message")
    args = ap.parse_args()

    pool = pool_dir()
    branch = branch_name()
    print(f"memory pool: {pool}  branch: {branch}")

    stage_and_commit(args.message, pool)
    run(["git", "-C", str(pool), "push", "origin", "HEAD"])

    slug = repo_slug(pool)
    existing = run(
        ["gh", "pr", "list", "--repo", slug, "--head", branch, "--json", "url", "-q", ".[0].url"],
        check=False,
    )
    if existing.stdout.strip():
        print(f"  PR already open: {existing.stdout.strip()}")
        return 0

    url = run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            slug,
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            args.message,
            "--body",
            f"Notes from {branch}",
        ]
    )
    print(f"  PR created: {url.stdout.strip()}")
    print("  → notify the Memory Arbiter that the PR is ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
