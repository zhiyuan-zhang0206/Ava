#!/usr/bin/env python3
"""Move the `latest` channel tag to a commit that passed the staging gate (L4, #1185).

Channel semantics: `latest` = the newest main commit that deployed to staging
AND passed the staging smoke gate. It is a rolling lightweight tag (force
moved), distinct from the dated release tags (v0.8.x-YYYYMMDD) that feed the
`stable` channel — release.yml's dated-tag trigger regex does not match
`latest`, so moving it never fires a release.

Guards (anti-silent-failure):
  - --sha must resolve to a commit that is reachable from origin/main
    (a tag can only point at code that actually landed).
  - The tag is only moved, never created from thin air: if `latest` does not
    exist yet the script prints the target and exits 0 without touching git
    (first deployment bootstraps the tag manually or via --create).

Usage:
    uv run python scripts/tag_latest.py --sha <sha>              # local move
    uv run python scripts/tag_latest.py --sha <sha> --push       # + push to origin
    uv run python scripts/tag_latest.py --sha <sha> --create     # create if missing
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TAG = "latest"


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — args are hardcoded git subcommands, never user input
        ["git", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=check,
    )


def sha_reachable_from_main(sha: str) -> bool:
    """True when `sha` is an ancestor of (or equal to) origin/main."""
    r = git("merge-base", "--is-ancestor", sha, "origin/main", check=False)
    if r.returncode == 0:
        return True
    # origin/main may be stale locally; fetch once and retry.
    git("fetch", "origin", "main", "--quiet")
    r = git("merge-base", "--is-ancestor", sha, "origin/main", check=False)
    return r.returncode == 0


def current_latest() -> str | None:
    r = git("rev-parse", "-q", "--verify", f"refs/tags/{_TAG}", check=False)
    return r.stdout.strip() or None


def move_latest(sha: str, *, create: bool = False) -> tuple[str | None, str]:
    """Move (or create) the tag; returns (old_sha, new_sha)."""
    old = current_latest()
    if old is None and not create:
        return old, sha
    if old is None:
        git("tag", _TAG, sha)
    else:
        git("tag", "-f", _TAG, sha)
    return old, sha


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True, help="commit sha that passed the staging gate")
    parser.add_argument("--push", action="store_true", help="push the moved tag to origin")
    parser.add_argument(
        "--create", action="store_true", help="create the tag when it does not exist yet"
    )
    args = parser.parse_args()

    if not sha_reachable_from_main(args.sha):
        print(f"[tag-latest] FAIL sha {args.sha} is not reachable from origin/main")
        return 1

    old, new = move_latest(args.sha, create=args.create)
    if old == new and old is not None:
        print(f"[tag-latest] no-op: latest already at {new}")
        return 0
    print(f"[tag-latest] latest {old or '(none)'} -> {new}")

    if args.push:
        r = git("push", "--force", "origin", f"refs/tags/{_TAG}")
        if r.returncode != 0:
            print(f"[tag-latest] FAIL push: {r.stderr.strip()[:300]}")
            return 1
        print(f"[tag-latest] pushed refs/tags/{_TAG} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
