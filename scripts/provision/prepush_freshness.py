"""At push, re-run the commit-stage artifact hooks whose inputs the branch only DELETED.

pre-commit never hands a deleted path to a hook, on any compared range, so a hook filtered by
`files:` is blind to a change that only deletes the last file behind a generated artifact: the
event registry, the config table or an OKF link target goes stale and nothing local notices.
Only a whole-repository run of the hook can see what a deletion left behind.

The nested branch-diff run (scripts/hooks/prepush-branch-lint.sh) already executes every commit-stage
hook whose inputs the branch added or changed, and a whole-repository hook (`pass_filenames:
false`) judges the deletion along with it. This covers the rest: a whole-repository hook whose
`files:` pattern matches a deleted path and no added or changed one, and a per-file hook (which
sees only the files it is handed) whose pattern matches any deleted path, runs with `--all-files`.
An unknown branch range is an explicit error: fetch origin/main before pushing.
CI re-checks every hook on the pushed and merged tree either way.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent.parent

# The generated-artifact family. tests/ci/test_prepush_hooks.py pins it against the config: a new
# `files:`-filtered artifact hook that does not join this list keeps the delete-only blind spot.
HOOKS = (
    "types-codegen-fresh",
    "constants-codegen-fresh",
    "events-registry-fresh",
    "config-lite-table-fresh",
    "lint-pyright-test-environments",
    "lint-ava-okf",
    "check-doc-references",
)


def _git(*args: str) -> str:
    return subprocess.run(  # noqa: S603 — fixed git queries in the repo root
        ["git", *args], cwd=_ROOT, capture_output=True, text=True, check=True
    ).stdout


def branch_paths() -> tuple[set[str], set[str]]:
    """(deleted, added-or-changed) paths of merge-base(origin/main, HEAD)..HEAD; raises when unknown.

    Renames are split into a deletion and an addition, so moving an input out of a hook's
    pattern counts as deleting it.
    """
    base = subprocess.run(  # noqa: S603 — fixed repo-local range owner
        ["bash", str(_ROOT / "scripts/hooks/prepush-base.sh")],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    listing = _git("diff", "--name-status", "--no-renames", "-z", base, "HEAD").split("\0")
    deleted: set[str] = set()
    other: set[str] = set()
    for status, path in zip(listing[0::2], listing[1::2], strict=False):
        (deleted if status == "D" else other).add(path)
    return deleted, other


def hooks_to_run(
    patterns: dict[str, tuple[re.Pattern[str], bool]], paths: tuple[set[str], set[str]]
) -> list[str]:
    """The hooks the nested run cannot cover (`patterns`: hook -> (files pattern, whole-repo)).

    Those whose pattern matches a deleted path and, for a whole-repository hook, no added or changed one (the nested run executes that hook
    over the whole repository anyway); a per-file hook never judges the deletion itself.
    """
    deleted, other = paths
    return [
        hook
        for hook, (pattern, whole_repo) in patterns.items()
        if any(pattern.search(p) for p in deleted)
        and not (whole_repo and any(pattern.search(p) for p in other))
    ]


def main() -> int:
    config = yaml.safe_load((_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    declared = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    patterns = {
        hook: (re.compile(declared[hook]["files"]), declared[hook].get("pass_filenames") is False)
        for hook in HOOKS
    }
    paths = branch_paths()
    due = hooks_to_run(patterns, paths)
    if not due:
        print(
            "pre-push: the branch deletes no input of a generated artifact on its own; nothing to add"
        )
    status = 0
    for hook in due:
        # types-codegen-fresh shells out to `npx --no-install openapi-typescript`, which needs
        # ui/web/node_modules: the same frontend-tooling dependency the frontend pre-push hooks
        # skip on. Everything else here is pure Python.
        if hook == "types-codegen-fresh" and not (_ROOT / "ui/web/node_modules").is_dir():
            print(
                f"WARNING: PRE-PUSH SKIPPED [{hook}]: missing ui/web/node_modules; "
                "run (cd ui/web && npm ci). CI must pass before merge.",
                file=sys.stderr,
            )
            continue
        run = subprocess.run(  # noqa: S603 — fixed argv, repo-local executable
            [".venv/bin/pre-commit", "run", "--hook-stage", "pre-commit", "--all-files", hook],
            cwd=_ROOT,
            check=False,
        )
        status = status or run.returncode
    return status


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        print(error.stderr or str(error), file=sys.stderr)
        raise SystemExit(error.returncode) from None
