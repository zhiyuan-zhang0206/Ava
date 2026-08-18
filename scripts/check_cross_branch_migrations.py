#!/usr/bin/env python3
"""Tripwire: fail if the single-branch premise this repo's migrations rest on breaks.

Run: `.venv/bin/python scripts/check_cross_branch_migrations.py` (exit 1 on a hit).

## Why a tripwire and not a check

Before the single-branch move (Model B, 2026-06-29) this script compared
`origin/main` against `origin/develop` to catch two things: a hotfix whose
migration never reached develop, and a migration patched differently on each
branch. With one branch neither is possible, so it became a pass-through that
always returned 0.

That is worse than absent. It kept its slot in `ci.yml`, so anyone reading that
file saw cross-branch migration drift apparently covered by a step that could
not fail for any reason.

The real gap was never the comparison — it was that **the single-branch
assumption was undocumented and unenforced**. Reintroduce a long-lived second
branch and the drift becomes possible again, while a pass-through keeps passing
precisely because it was written to skip when there is nothing to compare.

So it now asserts its own premise: it fails on exactly the condition that would
make the original comparison necessary again, and says to reinstate it. Same
cost, no longer a lie.

## What counts as a breach

An explicit name list, not a heuristic. Inferring "long-lived" from a branch's
age or commit count would fire on ordinary `ava-<id>-<task>` branches, which are
numerous and short-lived by policy. The names below are the ones that denote a
parallel integration line — the shape that reintroduces the drift.

`git ls-remote` is used rather than local refs because CI checks out a single
branch: `git branch -r` would see almost nothing and pass vacuously, which is
the same failure mode being fixed here.
"""

from __future__ import annotations

import subprocess
import sys

# Branch names denoting a parallel long-lived integration line. `release/`,
# `hotfix/` and `support/` are prefixes because they are conventionally
# versioned (`release/1.2`).
_BREACH_NAMES = frozenset({"develop", "development", "staging", "next", "trunk"})
_BREACH_PREFIXES = ("release/", "hotfix/", "support/")


def remote_branches() -> list[str]:
    """Branch names on `origin`, via `git ls-remote --heads`.

    Raises:
        RuntimeError: the remote could not be queried. Deliberately fatal — with
            no answer the premise is unverified, and reporting "no breach" on an
            error would rebuild the silent pass-through this replaced.
    """
    proc = subprocess.run(
        ["git", "ls-remote", "--heads", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-remote failed (rc={proc.returncode}): {proc.stderr.strip()}")
    names: list[str] = []
    for line in proc.stdout.splitlines():
        _sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/"):
            names.append(ref[len("refs/heads/") :])
    return names


def breaches(branches: list[str]) -> list[str]:
    """The branches whose existence would reinstate cross-branch migration drift."""
    return sorted(b for b in branches if b in _BREACH_NAMES or b.startswith(_BREACH_PREFIXES))


def main() -> int:
    try:
        branches = remote_branches()
    except RuntimeError as exc:
        print(f"cannot verify the single-branch premise: {exc}", file=sys.stderr)
        return 1

    found = breaches(branches)
    if found:
        print(
            "single-branch premise BROKEN — long-lived branch(es) on origin: "
            f"{', '.join(found)}\n\n"
            "Migrations assume one branch: names are timestamps and the applied set is\n"
            "keyed by name, so nothing detects the same migration patched differently on\n"
            "two lines, or a hotfix whose migration never reached the other one.\n\n"
            "Reinstate the origin/main vs origin/<branch> comparison this script used to\n"
            "perform (see its git history), or remove the branch.",
            file=sys.stderr,
        )
        return 1

    print(f"single-branch premise holds ({len(branches)} branch(es) on origin, none long-lived)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
