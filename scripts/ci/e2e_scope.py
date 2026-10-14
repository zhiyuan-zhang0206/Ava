#!/usr/bin/env python3
"""Whether a pull request can skip the e2e jobs: the rule is written here, not decided per run.

e2e is the integration net for product behavior, so it runs whenever either side changed. A
change that only edits unit tests inside packages (`<package>/**/tests/**/*.py`) cannot change
product behavior, and the backend selection already ran every test that can observe the edited
tests. The rule skips e2e exactly when all of these hold, with no further judgment:

- the event is a pull request, and not a Trunk queue branch (the queue always runs everything);
- test selection is enforced and its decision is SELECTED;
- every changed path is a Python file in a package-internal `tests` directory;
- none of those paths is one the e2e suite reaches by import (`e2e_reached_unit_tests`); a walk
  that cannot understand a file or an import form fails closed and never skips.

Top-level `tests/` is out of scope on purpose: e2e imports `tests.fixtures`, `tests.components`
and its own helpers, so a change there can reach e2e. A few test helpers live beside the
package that owns them and are shared with e2e (a polling helper, a login seeder); the rule
computes that set from e2e's own imports instead of assuming it is empty, so editing one of
them still runs e2e.

Standalone and stdlib-only: the classify job runs it without an environment.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from meizel.roles import packages  # noqa: E402 - standalone script

# The packages that carry their own `tests` directories: the application packages and the
# repository's own tooling (not `tests`, `ui`, the schedule templates or the demos).
PACKAGES = (*packages("application"), *packages("tooling"))
_PACKAGE_UNIT_TEST = re.compile(rf"(?:{'|'.join(PACKAGES)})/(?:[^/]+/)*tests/(?:[^/]+/)*[^/]+\.py")
_QUEUE_PREFIXES = ("trunk-merge/", "trunk-temp/")


def is_package_unit_test(path: str) -> bool:
    """Whether a repo-relative path is a Python file under a package-internal `tests` directory."""
    return _PACKAGE_UNIT_TEST.fullmatch(path) is not None


# Calls that import by name or by path. A literal module name is resolved like an import
# statement; anything else (a computed name, a path) is not understood, so it fails closed.
_NAMED_IMPORTS = {"import_module", "__import__", "run_module", "find_spec", "load_module"}
_UNDERSTOOD_NOTHING = {"run_path", "spec_from_file_location", "exec", "eval", "load_source"}


# The dynamic imports e2e is known to make, each read by a person: they run a product module or a
# generated program given on the command line, never a repository test file. Any other site fails
# closed, and `test_the_real_e2e_walk_understands_every_import` keeps this list honest.
_KNOWN_DYNAMIC: dict[str, frozenset[str]] = {
    "tests/e2e/process_support.py": frozenset({"run_module"}),  # the served daemon module
    "tests/e2e/fakes/child_budget_writer.py": frozenset({"exec"}),  # the generated scenario program
}


def stale_known_dynamic(root: Path, known: dict[str, frozenset[str]] | None = None) -> list[str]:
    """The `_KNOWN_DYNAMIC` entries whose call is gone: the file is missing or unparsable, or no
    longer makes a call of that name. A stale entry would quietly excuse a new site of the same
    name, so a test requires this to be empty and the entry gets deleted with the call."""
    stale: list[str] = []
    for relative, names in sorted((_KNOWN_DYNAMIC if known is None else known).items()):
        try:
            tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        except (SyntaxError, ValueError, OSError):
            stale.extend(
                f"{relative}: {name} (file missing or unreadable)" for name in sorted(names)
            )
            continue
        called = {_call_name(node) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        stale.extend(f"{relative}: {name}" for name in sorted(names) if name not in called)
    return stale


class _NotUnderstoodError(Exception):
    """A file e2e reaches that this rule cannot read or follow; the answer must be 'reached'."""


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _from_import_modules(node: ast.ImportFrom, package: tuple[str, ...], file: Path) -> list[str]:
    """The modules one `from ... import ...` names, a relative one resolved against `package`."""
    if node.level:
        if node.level > len(package) + 1:
            raise _NotUnderstoodError(f"{file}: a relative import climbs above the package")
        anchor = list(package[: len(package) - (node.level - 1)])
        module = ".".join([*anchor, *(node.module.split(".") if node.module else [])])
    else:
        module = node.module or ""
    return [module, *(f"{module}.{alias.name}" for alias in node.names)]


def _call_import(node: ast.Call, file: Path, root: Path) -> list[str]:
    """The module a call imports by name; raises for a call whose import cannot be read."""
    name = _call_name(node)
    if name in _KNOWN_DYNAMIC.get(file.relative_to(root).as_posix(), frozenset()):
        return []
    if name in _UNDERSTOOD_NOTHING:
        raise _NotUnderstoodError(
            f"{file}:{node.lineno}: {name}() imports what a reader cannot see"
        )
    if name not in _NAMED_IMPORTS:
        return []
    argument = node.args[0] if node.args else None
    if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
        raise _NotUnderstoodError(f"{file}:{node.lineno}: {name}() with a computed module name")
    return [argument.value]


def _imported_modules(tree: ast.AST, file: Path, root: Path) -> Iterable[str]:
    """Every module `tree` imports; raises `_NotUnderstoodError` for a form it cannot resolve."""
    package = file.relative_to(root).with_suffix("").parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield from _from_import_modules(node, package, file)
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            yield from _call_import(node, file, root)


def e2e_reached_unit_tests(root: Path) -> frozenset[str] | None:
    """Package-internal test files the e2e suite imports, directly or through other test modules.

    The walk starts at every `tests/e2e` file and follows imports into package-internal `tests`
    directories and into the top-level `tests/` helpers (e2e imports `tests.fixtures`, which in
    turn imports package test helpers). An edit to a file it finds can change what e2e does
    even though it sits in a package `tests` directory, so it never qualifies for the skip.

    Fail closed: a file that cannot be read or parsed, a computed or path-based import, or a
    relative import that cannot be resolved returns `None`, which means "assume everything is
    reached" and never skips e2e.
    """
    reached: set[str] = set()
    pending = sorted((root / "tests" / "e2e").rglob("*.py"))
    seen: set[Path] = set()
    try:
        while pending:
            file = pending.pop()
            if file in seen:
                continue
            seen.add(file)
            try:
                tree = ast.parse(file.read_text(encoding="utf-8"))
            except (SyntaxError, ValueError, OSError) as error:
                raise _NotUnderstoodError(f"{file}: {error}") from error
            for module in _imported_modules(tree, file, root):
                if module.split(".")[0] not in (*PACKAGES, "tests"):
                    continue
                base = Path(*module.split("."))
                for candidate in (base.with_suffix(".py"), base / "__init__.py"):
                    relative = candidate.as_posix()
                    if not (root / candidate).is_file():
                        continue
                    if is_package_unit_test(relative):
                        reached.add(relative)
                    if is_package_unit_test(relative) or relative.startswith("tests/"):
                        pending.append(root / candidate)
    except _NotUnderstoodError:
        return None
    return frozenset(reached)


def skips_e2e(
    *,
    event: str,
    head_ref: str,
    mode: str,
    decision: str,
    changed: list[str],
    reached: frozenset[str] | None = frozenset(),
) -> bool:
    """The whole rule: see the module docstring. `reached` is `e2e_reached_unit_tests`;
    `None` (the walk could not understand something) never skips."""
    return (
        event == "pull_request"
        and not head_ref.startswith(_QUEUE_PREFIXES)
        and mode == "enforce"
        and decision == "SELECTED"
        and bool(changed)
        and all(is_package_unit_test(path) for path in changed)
        and reached is not None
        and reached.isdisjoint(changed)
    )


def main(argv: list[str] | None = None) -> int:
    """Print `skip=true` or `skip=false` for the workflow's step output."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--mode", default="")
    parser.add_argument("--decision", default="")
    args = parser.parse_args(argv)
    changed = [line.strip() for line in args.changed_files.read_text().splitlines() if line.strip()]
    skip = skips_e2e(
        event=args.event,
        head_ref=args.head_ref,
        mode=args.mode,
        decision=args.decision,
        changed=changed,
        reached=e2e_reached_unit_tests(_REPOSITORY_ROOT),
    )
    print(f"skip={str(skip).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
