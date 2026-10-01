"""Keep a test out of the top-level `tests/` unless it is registered there.

Run: `.venv/bin/python scripts/structure/tests_location.py [path ...]` (no argument checks every
tracked `tests/**/test_*.py`; an explicit path that does not exist is an error (stderr + exit 1)).
`--suggest PATH ...` prints where a test belongs and exits 0: the one mode that reads the
production code (`scripts/structure/tests_location_suggest.py`); the checks never do. Also run via
pre-commit and in the CI structure job (`pre-commit run --all-files`).

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
2. it is in `ALLOWED` (same file) with a category and a one-line reason: `contract` (it reads
   repository artifacts no package owns: workflows, `pyproject.toml`, `db/schema.sql`,
   migrations, `ui/`, `schedules/`, skill scripts, the test harness itself, or scans the whole
   tree) or `integration` (it spans units that may not import each other, so no package may hold
   it);
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
that may legally hold it, or says why none can), with `git mv`. A test moved into a directory that
`tests/fixtures/path_scopes.py` does not list silently loses the autouse isolation fixtures its
old directory had: list the new directory there (`tests/ci/test_path_scopes.py` fails when it is
missing). A test that cannot live in a package is registered in `ALLOWED`, `contract` or
`integration`, with a reason a reviewer can check.

## Scope and cost

Only paths: the checked files, the registry and the baseline shards are read; no module index, no
import graph, no `place()`. A hook run costs a process start plus reading the shards. The rule's
inputs (this file, the registry module, the suggestion module, any baseline shard) re-check every
test; the pre-push hook and the CI structure job check every tracked top-level test. Whether a
registered test still has a package home (a test frozen as "to move" may by now sit at its home's
legal top) is deliberately not checked here: it needs the placement rule, which is not
sub-second.
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
_RULE_INPUTS = (
    "scripts/structure/tests_location.py",
    "scripts/structure/tests_location_allowed.py",
    "scripts/structure/tests_location_suggest.py",
)
_CATEGORIES = ("contract", "integration")

_SUGGEST = "--suggest"
_GUIDE = (
    "A top-level test must move into the tests/ directory of the package it tests (`git mv`), "
    "unless it is registered:\n"
    "  - tests/fixtures/path_scopes.py: if it lists the test's old directory, list the new "
    "directory there too, or the autouse isolation fixtures silently stop applying to the "
    "moved test (tests/ci/test_path_scopes.py fails).\n"
    "  - scripts/structure/tests_location_allowed.py: a test that cannot live in a package is "
    "registered as `contract` (it reads repository artifacts no package owns, or scans the "
    "whole tree) or `integration` (it spans units that may not import each other), with a "
    "one-line reason.\n"
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


def _explicit_files(argv: list[str], repo_root: Path) -> tuple[list[str], bool] | None:
    """(checked top-level tests, whether the run must be full) for explicit paths.

    None when a path does not exist. A path that is no test at all (this lint, its registry, a
    baseline shard) changes what every test is judged against: full run. A test that is not a
    top-level one is not this lint's business.
    """
    resolved = [lint_common.resolve_targets([arg], repo_root) for arg in argv]
    missing = [arg for _, bad in resolved for arg in bad]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return None
    names = sorted({rel for files, _ in resolved for rel in files})
    full = any(not lint_common.is_test_path(rel) for rel in names)
    return [rel for rel in names if is_top_level_test(rel)], full


def _files_to_check(argv: list[str], repo_root: Path) -> list[str] | None:
    """The tests to judge: every tracked one, or the top-level tests the arguments name."""
    if not argv:
        return _tracked_tests(repo_root)
    explicit = _explicit_files(argv, repo_root)
    if explicit is None:
        return None
    files, full = explicit
    return _tracked_tests(repo_root) if full else files


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
    allowed = tests_location_allowed.ALLOWED if allowed is None else allowed
    files = _files_to_check(argv, repo_root)
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
