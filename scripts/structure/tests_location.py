"""Keep a test out of the top-level `tests/` unless it is registered there.

Run: `.venv/bin/python scripts/structure/tests_location.py [path ...] [--only FILE ...]` (no
argument checks every tracked `tests/**/test_*.py`; explicit paths judge exactly those tests; an
explicit path that does not exist is an error (stderr + exit 1)). `--only FILE ...` is the commit
hook's changed-files mode (`scripts/lint/docs/changed-files-mode.ava.okf.md`): it judges the
changed top-level tests, and a changed lint tool, registry or baseline shard (everything under
`scripts/structure/`) widens it to every tracked test. `--suggest PATH ...` prints where a test
belongs and exits 0: the one mode that reads the production code
(`scripts/structure/tests_location_suggest.py`); the checks never do. Also run via pre-commit and
in the CI structure job (`pre-commit run --all-files`).

## Why

A test belongs in the `tests/` directory of the package it proves (`<pkg>/**/tests/`), where the
package's owner sees it change with the code. While tests moved into packages, new ones kept
landing in the top-level `tests/` (29 new top-level test files in the 36 hours to 2026-10-01, 20
of them moved or deleted again afterwards), and every such file costs a second move later: the
flaky-test quarantine and `.test_durations` are keyed by path, so a move resets both. Nothing
refused the file at the door.

## The rule

A top-level test is a `test_*.py` file anywhere under `tests/`. It may stay only when one of these
holds, all decided from its path alone:

1. its directory or file is in `BY_DESIGN` (`scripts/structure/tests_location_allowed.py`): the
   end-to-end tests, the browser UI tests, the shared fixtures and factories, the real-process
   proofs, none of which has a package to live in;
2. it is in `ALLOWED` (same file) with a category and a one-line reason: `contract` (the
   test's subject is a repository artifact (workflows, `pyproject.toml`, the schema, migrations,
   `ui/`, `schedules/`, skill scripts) or the test harness itself, which no package owns; a scan
   over the whole tree counts) or `integration` (it spans units that may not import each other,
   so no package may hold it);
3. its `path::top-level` key is frozen in the `tests_location` section of the structure baseline
   shards (`scripts/structure/baseline/`): the debt of tests still to move. The section is
   shrink-only against the base revision and a renamed file carries its key
   (`scripts/lint/code_structure.py`).

Any other top-level test is a violation. The verdict never looks at what the test imports, so it
cannot move with an unrelated production commit, and the check is a set lookup per file. A
registered file that no longer exists, a baseline key or `ALLOWED` entry that a registered file
does not need (it sits under `BY_DESIGN`, or is listed twice), a malformed entry: all fail, so
the registry cannot rot into a permit wall.

## Fixing a violation

Move the test into the `tests/` directory of the package it tests
(`.venv/bin/python scripts/structure/tests_location.py --suggest <file>` names the lowest package
that may legally hold it, or says why none can), with `git mv`. A test moved into a directory whose
`path_scopes.toml` does not name it silently loses the autouse isolation fixtures its old
directory had: name it in the new directory's `path_scopes.toml`
(`tests/ci/test_path_scopes.py` fails when it is missing). A test that cannot live in a package is registered in `ALLOWED`, `contract` or
`integration`, with a reason a reviewer can check.

## Scope and cost

Only paths, and only relative ones: every judgment is on the repo-relative POSIX path of a tracked
file, never on where the checkout sits (a repository under `/tmp/...` or inside a `tests/` or
`e2e/` directory gets the same verdicts). The checked files, the registry and the baseline shards
are read; no module index, no import graph, no `place()`. A commit hook (`--only`) costs a process
start plus reading the shards, in proportion to the changed test files; the registry's entries are
checked for existence on every run (a stat each). The pre-push hook and CI's `backend-structure`
(`pre-commit run --all-files`) check every tracked top-level test, which is also where a deleted
or renamed test's stale entry is found when no commit hook saw it. Whether a registered test still
has a package home (a test frozen as "to move" may by now sit at its home's legal top) is
deliberately not checked here: it needs the placement rule, which is not sub-second.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import (  # noqa: E402 — standalone script
    baseline_shards,
    lint_common,
    tests_location_allowed,
)

SECTION = "tests_location"
FROZEN_TARGET = "top-level"
_CATEGORIES = ("contract", "integration")

_SUGGEST = "--suggest"
_GUIDE = (
    "A top-level test must move into the tests/ directory of the package it tests (`git mv`), "
    "unless it is registered:\n"
    "  - path_scopes.toml: if the test's old directory has one that names the test, name it in the "
    "new directory's path_scopes.toml too, or the autouse isolation fixtures silently stop "
    "applying to the moved test (tests/ci/test_path_scopes.py fails; see "
    "tests/fixtures/path_scopes.py).\n"
    "  - scripts/structure/tests_location_allowed.py: a test that cannot live in a package is "
    "registered as `contract` (its subject is a repository artifact or the test harness itself, "
    "which no package owns) or `integration` (it spans units that may not import each other), "
    "with a one-line reason.\n"
    "Rule: scripts/structure/tests_location.py."
)


def is_top_level_test(rel: str) -> bool:
    """Whether a repo-relative POSIX path is a `test_*.py` file under the top-level `tests/`."""
    parts = rel.split("/")
    return (
        len(parts) >= 2
        and parts[0] == "tests"
        and parts[-1].startswith("test_")
        and parts[-1].endswith(".py")
    )


def stays_by_design(rel: str) -> str | None:
    """The reason a top-level path has no package to move to, or None."""
    for path, reason in tests_location_allowed.BY_DESIGN.items():
        if rel == path or (path.endswith("/") and rel.startswith(path)):
            return reason
    return None


def frozen_key(rel: str) -> str:
    return f"{rel}::{FROZEN_TARGET}"


def read_baseline(repo_root: Path) -> dict[str, int]:
    """The frozen `tests_location` entries of every baseline shard, validated like `merge`.

    Raises ValueError on a malformed entry: a key is `<top-level test path>::top-level` and its
    value is 1.
    """
    texts: dict[str, str] = {}
    for name, text in baseline_shards.read_worktree(repo_root).items():
        shard = cast("object", json.loads(text))
        if isinstance(shard, dict) and (section := cast("dict[str, object]", shard).get(SECTION)):
            texts[name] = json.dumps({SECTION: section})
    merged = baseline_shards.merge(texts, [SECTION])[SECTION]
    for key, count in merged.items():
        path, separator, target = key.partition("::")
        if not (is_top_level_test(path) and separator and target == FROZEN_TARGET and count == 1):
            raise ValueError(
                f"invalid {SECTION} entry {key!r}: expected `tests/.../test_x.py::{FROZEN_TARGET}` "
                "with the value 1"
            )
    return merged


def _entry_errors(where: str, rel: str, repo_root: Path) -> list[str]:
    """What is wrong with one registered path, whichever registry holds it."""
    if not is_top_level_test(rel):
        return [f"{where}: `{rel}` is not a top-level test file (tests/**/test_*.py)"]
    if (reason := stays_by_design(rel)) is not None:
        return [f"{where}: `{rel}` needs no entry: it stays at the top level by design ({reason})"]
    if not (repo_root / rel).is_file():
        return [f"{where}: stale entry `{rel}`: the file no longer exists — remove the entry"]
    return []


def registry_errors(
    allowed: Mapping[str, tuple[str, str]], baseline: dict[str, int], repo_root: Path
) -> list[str]:
    """Stale, needless, duplicate and malformed entries of `ALLOWED` and the frozen baseline."""
    errors: list[str] = []
    frozen = {key.partition("::")[0] for key in baseline}
    source = "scripts/structure/tests_location_allowed.py"
    for rel, (category, reason) in sorted(allowed.items()):
        if category not in _CATEGORIES:
            errors.append(f"{source}: `{rel}` has category {category!r}: expected {_CATEGORIES}")
        if not reason.strip():
            errors.append(f"{source}: `{rel}` has no reason")
        errors.extend(_entry_errors(source, rel, repo_root))
        if rel in frozen:
            errors.append(f"{source}: `{rel}` is also frozen in the {SECTION} baseline — keep one")
    for rel in sorted(frozen - allowed.keys()):
        errors.extend(
            _entry_errors(baseline_shards.shard_path(SECTION, frozen_key(rel)), rel, repo_root)
        )
    return errors


def unregistered_errors(
    files: list[str], allowed: Mapping[str, tuple[str, str]], baseline: dict[str, int]
) -> list[str]:
    """One message per top-level test that is neither by design, allowed nor frozen."""
    return [
        f"{rel}:1: a top-level test that is not registered: move it into the tests/ directory of "
        f"the package it tests (`.venv/bin/python scripts/structure/tests_location.py --suggest {rel}`)"
        for rel in files
        if is_top_level_test(rel)
        and stays_by_design(rel) is None
        and rel not in allowed
        and frozen_key(rel) not in baseline
    ]


def _tracked_tests(repo_root: Path) -> list[str]:
    """Every tracked path under `tests/` (git's index: nothing is walked)."""
    out = subprocess.run(  # noqa: S603 — fixed argv, script-derived repo root
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", "tests"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [path for path in out.stdout.split("\0") if path]


def _named_tests(argv: list[str], repo_root: Path) -> list[str] | None:
    """The top-level tests the explicit paths name; None when a path does not exist."""
    files, missing = lint_common.resolve_targets(argv, repo_root)
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return None
    return [rel for rel in files if is_top_level_test(rel)]


def _files_to_check(argv: list[str], only: list[str] | None, repo_root: Path) -> list[str] | None:
    """The tests to judge: the explicit paths or every tracked test, narrowed to `--only`'s
    changed files unless a changed path is lint tooling (then the whole default set)."""
    scope = lint_common.changed_scope(only, repo_root)
    if argv:
        named = _named_tests(argv, repo_root)
        return None if named is None else [rel for rel in named if scope is None or rel in scope]
    if scope is None:
        return _tracked_tests(repo_root)
    return sorted(rel for rel in scope if is_top_level_test(rel))


def _suggest(argv: list[str], repo_root: Path) -> int:
    # Heavy (placement, the import graph): imported only when a suggestion is asked for.
    from scripts.structure import tests_location_suggest

    files, missing = lint_common.resolve_targets(argv, repo_root)
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    for rel in files:
        print(tests_location_suggest.suggest(rel, repo_root))
    return 0


def main(
    argv: list[str] | None = None,
    *,
    repo_root: Path = _REPO_ROOT,
    allowed: Mapping[str, tuple[str, str]] | None = None,
) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    repo_root = repo_root.resolve()
    if argv[:1] == [_SUGGEST]:
        return _suggest(argv[1:], repo_root)
    argv, only = lint_common.split_only(argv)
    if only == []:
        return 0  # nothing changed, nothing to judge
    allowed = tests_location_allowed.ALLOWED if allowed is None else allowed
    files = _files_to_check(argv, only, repo_root)
    if files is None:
        return 1
    try:
        baseline = read_baseline(repo_root)
    except (OSError, ValueError) as exc:
        print(f"scripts/structure/baseline: invalid {SECTION} baseline: {exc}", file=sys.stderr)
        return 1
    errors = [
        *unregistered_errors(files, allowed, baseline),
        *registry_errors(allowed, baseline, repo_root),
    ]
    for error in errors:
        print(error)
    if errors:
        print(f"\n{len(errors)} tests-location violations. {_GUIDE}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
