"""Per-machine steward: stage, commit, push, create PR.

Usage: python3 steward.py -m "memory: <machine> <date> local sync"

The multi-host flow: each machine runs this on its own checkout, creating a
PR from machine-<name> → main. It never merges — that is the arbiter's job.
The runner refuses to start outside this machine's own branch: the push
carries whatever branch the checkout is on, so a run from `main` would push
straight to main and bypass the PR flow.
After it returns, message the Memory Arbiter that the PR is ready.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import branch_name, current_branch, pool_dir, repo_slug, run, stage_and_commit


def main() -> int:
    ap = argparse.ArgumentParser(description="Per-machine memory steward sync")
    ap.add_argument("-m", "--message", required=True, help="commit message")
    args = ap.parse_args()

    pool = pool_dir()
    branch = branch_name()
    print(f"memory pool: {pool}  branch: {branch}")

    current = current_branch(pool)
    if current != branch:
        print(
            f"✗ refusing: the checkout is on {current or 'a detached HEAD'}, not {branch} —\n"
            f"  the steward pushes whatever branch is checked out (a run from `main`\n"
            f"  would push straight to main). Check out this machine's branch first:\n"
            f"    git -C {pool} checkout -B {branch} origin/main",
            file=sys.stderr,
        )
        return 2

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
