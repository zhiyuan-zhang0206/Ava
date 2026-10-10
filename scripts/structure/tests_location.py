"""Keep root tests only when their complete subjects prove root or policy already owns them.

Run `.venv/bin/python scripts/structure/tests_location.py [path ...] [--only FILE ...]`.
No arguments check tracked top-level `tests/**/test_*.py`; named missing paths fail.
The changed-files hook judges changed root tests; a structure-tool change widens it
to the tracked root tests. Pre-push and CI run the same rule over all files.

Existing BY_DESIGN and ALLOWED entries retain their path policy, including stale,
needless and malformed-entry rejection. An unregistered root test instead needs a
complete subject-LCA proof from `placement_evidence.subject_lca()`: resolved strong Python references,
without replacement-only evidence or test support, must span directories whose
common ancestor is the repository root. Unknown dependencies, empty subjects,
sample strings and the legacy all-patch fallback never certify placement.

This query consumes the shared facts and module resolver, not production import
direction or the legacy private-patch home heuristic. Known resources alone do not
prove a Python subject; unresolved resource inputs still prevent certification.
It reads only a candidate's source and exact dependency locations, not all runtime
fixture dependencies or the repository's reverse impact closure. Registered and
by-design tests retain the path-only fast path. No new registration is required
for a genuine cross-package subject proof, and no baseline is introduced.

A rejected single-component root test must move into its subject directory's
`tests/`. `--suggest PATH ...` explains this same proof. After `git mv`, preserve
its autouse fixtures in the destination's `path_scopes.toml`. Package-local tests
are outside this gate's scope; this change does not launch a whole-tree migration.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import (  # noqa: E402 — standalone script
    lint_common,
    tests_location_allowed,
)

_CATEGORIES = ("contract", "integration")

_SUGGEST = "--suggest"
_GUIDE = (
    "A top-level test needs complete subject evidence whose LCA is root. Otherwise use `git mv` "
    "into its subject's tests/ directory and preserve path_scopes.toml isolation fixtures. "
    "Verify the moved test's resolved fixtures and teardown; path_scopes validation alone "
    "does not prove that an old binding followed the move. Unknown inputs must be resolved; "
    "replacement-only and sample evidence cannot certify placement. "
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


def _entry_errors(where: str, rel: str, repo_root: Path) -> list[str]:
    """What is wrong with one registered path, whichever registry holds it."""
    if not is_top_level_test(rel):
        return [f"{where}: `{rel}` is not a top-level test file (tests/**/test_*.py)"]
    if (reason := stays_by_design(rel)) is not None:
        return [f"{where}: `{rel}` needs no entry: it stays at the top level by design ({reason})"]
    if not (repo_root / rel).is_file():
        return [f"{where}: stale entry `{rel}`: the file no longer exists — remove the entry"]
    return []


def registry_errors(allowed: Mapping[str, tuple[str, str]], repo_root: Path) -> list[str]:
    """Stale, needless and malformed entries of `ALLOWED`."""
    errors: list[str] = []
    source = "scripts/structure/tests_location_allowed.py"
    for rel, (category, reason) in sorted(allowed.items()):
        if category not in _CATEGORIES:
            errors.append(f"{source}: `{rel}` has category {category!r}: expected {_CATEGORIES}")
        if not reason.strip():
            errors.append(f"{source}: `{rel}` has no reason")
        errors.extend(_entry_errors(source, rel, repo_root))
    return errors


def unregistered_errors(
    files: list[str], allowed: Mapping[str, tuple[str, str]], repo_root: Path
) -> list[str]:
    """Unregistered root tests must prove root from complete, resolved Python subjects."""
    candidates = [
        rel
        for rel in files
        if is_top_level_test(rel) and stays_by_design(rel) is None and rel not in allowed
    ]
    if not candidates:
        return []
    from scripts.structure import placement, placement_evidence

    index = placement.ModuleIndex(repo_root)
    errors: list[str] = []
    for rel in candidates:
        tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"), filename=rel)
        result = placement_evidence.subject_lca(tree, rel, index)
        if result.directory == "":
            continue
        if result.unknown:
            detail = "incomplete subject evidence: " + "; ".join(
                f"{u.path}:{u.line}: {u.reason}" for u in result.unknown
            )
        elif result.directory is None:
            detail = "no subject proves a root LCA"
        else:
            detail = f"subject LCA is {result.directory}; move into {result.directory}/tests/"
        errors.append(
            f"{rel}:1: a top-level test that is not registered has no root proof: {detail} "
            f"(`.venv/bin/python scripts/structure/tests_location.py --suggest {rel}`)"
        )
    return errors


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
    errors = [*unregistered_errors(files, allowed, repo_root), *registry_errors(allowed, repo_root)]
    for error in errors:
        print(error)
    if errors:
        print(f"\n{len(errors)} tests-location violations. {_GUIDE}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
