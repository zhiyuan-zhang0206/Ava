"""Arbiter merge: squash-merge all open PRs targeting main, then refresh.

Usage: python3 arbiter_merge.py

Run by the Memory Arbiter after all per-machine stewards have reported their
PRs ready. Merges PRs one at a time (each merge advances main, which may make
later PRs conflict — those are skipped with a warning), then refreshes the
index. Refresh is bundled deliberately: merging without refreshing is what
caused the 2026-06-22 → 2026-08-01 index staleness incident. Exit code 1 if
any PR was skipped or the refresh failed — treat that as an alert.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import pool_dir, refresh_index, repo_slug, run


def main() -> int:
    pool = pool_dir()
    slug = repo_slug(pool)

    listed = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--base",
            "main",
            "--json",
            "number",
            "-q",
            ".[].number",
        ],
        check=False,
    )
    numbers = [int(n) for n in listed.stdout.split()]
    if not numbers:
        print("  (no open PRs)")
        return 0

    merged: list[int] = []
    skipped: list[tuple[int, str]] = []
    for n in numbers:
        r = run(
            ["gh", "pr", "merge", str(n), "--repo", slug, "--squash", "--delete-branch"],
            check=False,
        )
        if r.returncode == 0:
            merged.append(n)
            print(f"  merged PR #{n}")
        else:
            reason = r.stderr.strip() or str(r.returncode)
            skipped.append((n, reason))
            print(f"  ✗ PR #{n} merge failed: {reason}", file=sys.stderr)

    print(f"\n  merged: {len(merged)}  skipped: {len(skipped)}")
    rc = 1 if skipped else 0

    if merged:
        try:
            refresh_index()
        except SystemExit:
            print(
                "  ✗ post-merge refresh FAILED — the index is stale until it succeeds; "
                "run `ava memory refresh` manually once the gateway is reachable",
                file=sys.stderr,
            )
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
