from __future__ import annotations

import sys

from meiva_builtins.plugins.meiva_memory.pool_ops import pool_dir, refresh_index, repo_slug, run


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
                "run `meiva memory refresh` manually once the gateway is reachable",
                file=sys.stderr,
            )
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
