"""Forbid a test from patching a private name of a package it does not belong to.

Run: `.venv/bin/python scripts/lint/patch_targets.py [path ...]` (no argument scans every
test file; an explicit path that does not exist is an error (stderr + exit 1)). `--report`
prints the census behind the rule (class distribution, violations by relation, the most
patched private modules) as Markdown and always exits 0. Also run via pre-commit and in the
CI structure job (`pre-commit run --all-files`).

## Why

A test that replaces `ava.mcps._daemon._connect_server` depends on an implementation detail
its owner never promised to keep, and every such reach-in is a missing injection seam: the
code under test had no public way to take its clock, its transport or its identity from the
caller, so the test reached in. Rule 4 (`scripts/lint/code_structure.py`) forbids the same
reach for imports and attribute access but exempts test directories wholesale, so a string
target such as `monkeypatch.setattr("base.db.connections._pool", ...)` was invisible.

## The rule

Every patch point of a test file is classified (`scripts/structure/patch_targets.py`
documents classes A-E and U). A point is a violation (class D) when all of these hold:

1. the target is this repository's code (not stdlib, third-party or the runtime);
2. it is not in the ambient-environment list `E_MODULES` (settings, paths, machine and
   cluster identity, env resolution, ambient services, `AVA_*` variables);
3. its name is private (an attribute or module segment starting with one underscore), and
   the package that owns it (Rule 4's owner: the package holding the first private
   component) does not contain the test's *home*.

The home is the package the file's own first-party references place it in, not the
directory it sits in (`scripts/structure/placement.py`), so the verdict is the same before
and after a test moves into `<pkg>/tests/`. It is the deepest package that holds or directly
depends on every module the file references: a test of `cli.commands.cluster.health` that
also references `cli.commands._probe` lives in `cli/commands/cluster` when `health.py`
imports `_probe`, and a private name of that package is then its own. Recognised forms:
`monkeypatch.setattr / delattr / setitem` (string target or object plus attribute name),
`patch`, `patch.object`, `patch.dict`, `patch.multiple`, `mocker.patch`, as calls, decorators
or `with` blocks. Deep attributes of another package's public name (`module.Class.method`)
are counted in the report but are not violations yet; a target the linter cannot resolve
statically is counted, never flagged.

## Fixing a violation

The message names the package that owns the private name and the relation of the test to it:

- the test lives in an ancestor package (its imports span several packages) and patches a
  descendant's private name: move the test down into the owning package (a test that also
  needs a package the owner does not import cannot sit there: split the file), or
- give the owning package a public entry point or injection seam (a parameter, a settings
  field, a public setter) and patch that.

Every foreign-private patch fails directly. There is no per-site opt-out or
baseline allowance; moving a file or changing a measurement rule cannot permit
one. Current structure shards reject the retired `patch_targets` field, even
when it is empty.

## Scope and cost

Scans `tests/` and every `**/tests/` under the governed packages. A file's result depends on
its own text, `pyproject.toml` and the direct imports of the non-test source (the home
follows what the subject's package imports), so a production import change can move the home
of a test nobody touched. The checks split accordingly:

- pre-commit (`lint-patch-targets`) passes the changed test files and only those are
  checked; a changed lint script, placement module or baseline shard triggers a full scan;
- pre-push (`lint-patch-targets-full`) and the CI structure job scan everything, so a
  production import change is caught before it merges.

Production imports are read from the working tree on every run (about 0.4 s warm, 1.5-2.5 s cold) and
cached per file in `.cache/structure/`; no dependency graph is committed.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import (  # noqa: E402 — standalone script
    lint_common,
    locality,
    patch_report,
    patch_targets,
)
from scripts.structure.placement import CODE_TOPS, ModuleIndex  # noqa: E402 — standalone script

_HINT_MODULES = 5


def all_test_files(repo_root: Path) -> list[Path]:
    """Every test-tree Python file: the top-level `tests/` and each package's `tests/`."""
    roots = [repo_root / "tests"]
    for top in CODE_TOPS:
        roots.extend(d for d in (repo_root / top).rglob("tests") if d.is_dir())
    return sorted({p for root in roots for p in root.rglob("*.py") if "__pycache__" not in p.parts})


def _explicit_targets(argv: list[str], repo_root: Path) -> tuple[list[Path], bool] | None:
    """(scanned test files named by the arguments, whether the run must be full).

    None when an argument does not exist. A file that is neither a scanned test nor any
    test (the lint, its placement rule, a baseline shard) means the rule changed: full scan.
    A test file outside the scanned scope is ignored, as the full run ignores it.
    """
    missing = [arg for arg in argv if not Path(arg).exists()]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return None
    scope = set(all_test_files(repo_root))
    files: set[Path] = set()
    full = False
    for arg in argv:
        path = Path(arg).resolve()
        for member in sorted(path.rglob("*.py")) if path.is_dir() else [path]:
            if member in scope:
                files.add(member)
            elif path.is_file() and not lint_common.is_repo_test_file(member, repo_root):
                full = True
    return sorted(files), full


def _hint(measured: dict[str, int]) -> str:
    modules: dict[str, int] = {}
    for key, count in measured.items():
        module = key.partition("::")[2].rsplit(".", 1)[0]
        modules[module] = modules.get(module, 0) + count
    top = sorted(modules.items(), key=lambda item: (-item[1], item[0]))[:_HINT_MODULES]
    listed = ", ".join(f"{module} ({count})" for module, count in top)
    return (
        f"Most patched private targets: {listed}. A shared public injection seam for one "
        "of these is cheaper than a private patch per test; `scripts/lint/patch_targets.py "
        "--report` lists the full ranking."
    )


def _select_files(argv: list[str], repo_root: Path) -> list[Path] | None:
    """The files to scan: every test file, or the test files the arguments name.

    None when an argument does not exist. A non-test file argument (the lint, its placement
    rule, a baseline shard) means the rule itself changed, so the whole tree is scanned.
    """
    if not argv:
        return all_test_files(repo_root)
    explicit = _explicit_targets(argv, repo_root)
    if explicit is None:
        return None
    files, full = explicit
    return all_test_files(repo_root) if full else files


def _scan(
    files: list[Path], repo_root: Path
) -> tuple[dict[str, patch_targets.FileResult], patch_targets.Sites, list[str]]:
    """(analysed files with patch points, measured class D sites, per-site errors)."""
    locality.reset_caches()
    classifier = patch_targets.Classifier(ModuleIndex(repo_root))
    results: dict[str, patch_targets.FileResult] = {}
    measured: patch_targets.Sites = {}
    errors: list[str] = []
    for path in files:
        rel = path.relative_to(repo_root).as_posix()
        try:
            result = patch_targets.analyze(rel, path.read_text(encoding="utf-8"), classifier)
        except (OSError, UnicodeDecodeError):
            continue  # unreadable member, as every lint skips it
        except SyntaxError as exc:
            errors.append(f"{rel}:{exc.lineno or 1}: cannot parse: {exc.msg}")
            continue
        if result.sites:
            results[rel] = result
        measured.update(patch_targets.violations(rel, result))
        errors.extend(patch_targets.site_errors(rel, result))
    return results, measured, errors


def main(argv: list[str] | None = None, *, repo_root: Path = _REPO_ROOT) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    repo_root = repo_root.resolve()
    report = "--report" in argv
    argv = [arg for arg in argv if arg != "--report"]
    files = _select_files([] if report else argv, repo_root)
    if files is None:
        return 1
    results, measured, errors = _scan(files, repo_root)
    if report:
        print(patch_report.render(results))
        return 0
    for error in errors:
        print(error)
    if errors:
        counts = {key: len(lines) for key, lines in measured.items()}
        print(f"\n{len(errors)} patch-target violations. {_hint(counts)}", file=sys.stderr)
        print("Rule and fixes: see scripts/lint/patch_targets.py.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
