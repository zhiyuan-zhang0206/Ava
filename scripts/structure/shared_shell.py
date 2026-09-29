"""The `shared/` release-probe shell: three whitelisted files that nothing imports.

The `shared` package was renamed `base`. What remains under `shared/` is a
time-boxed shell for release preparation across versions (the why and the
retirement condition live in shared/__init__.py). Rule 7 in
scripts/lint/code_structure.py keeps it from growing back into a package:

- `shared/` holds nothing but `SHELL_FILES` (`__pycache__` aside); deleting the
  shell early is caught by the probe contract tests, not here;
- no Python file in the repository imports `shared` (an `import` / `from`
  statement, or `importlib.import_module` / `__import__` naming it) - tests
  included, since the shell's contract is only ever exercised through the probe
  strings, never through an import;
- outside test directories, a string literal names the shell (a `shared.<name>`
  module path, or the `shared/release-build.json` identity member) only at the
  sites in `ALLOWED_MENTIONS`, each with its exact count, so a stale or a new
  mention fails (an entry is checked while its file exists). Docstrings,
  comments and prose are not code and are not checked. `PINNED_ELSEWHERE`
  files run against another revision's tree and are exempt.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from scripts.structure import lint_common

SHELL_DIR = "shared"
SHELL_FILES = frozenset(
    {"shared/__init__.py", "shared/runtime_abi.py", "shared/runtime_plugins.py"}
)
# Non-test code that may name the shell in a string literal: (file, mention) -> count.
ALLOWED_MENTIONS: dict[tuple[str, str], int] = {
    # The two release probes, run inside the TARGET image's own interpreter.
    ("base/deploy/release/runtime_prepare.py", "shared.runtime_abi"): 1,
    ("base/deploy/release/runtime_prepare.py", "shared.runtime_plugins"): 1,
    # The builder-written identity member, read from images other versions built.
    ("base/deploy/release/identity.py", "shared/release-build.json"): 1,
}
# Python run against another revision's tree, never this one: the legacy proof
# script executes inside the reconstructed pinned 612326d install, and the
# preparation driver imports its helpers from the pinned preparation-tools
# checkout (scripts/legacy_lkg/manifest.json). Their `shared` is that tree's.
PINNED_ELSEWHERE = frozenset({"scripts/legacy_lkg/cold_boot.py", "scripts/legacy_lkg/prepare.py"})
_SELF = "scripts/structure/shared_shell.py"  # the allowlist above names what it allows
_MENTION = re.compile(
    r"(?<![\w./-])shared(?:\.[A-Za-z_]\w*)+|(?<![\w./-])shared/release-build\.json"
)
_TEST_DIR = re.compile(r"(^|/)tests?/")
_IMPORTERS = frozenset({"import_module", "__import__"})


def _names_shell(module: str | None) -> bool:
    return module is not None and (module == SHELL_DIR or module.startswith(SHELL_DIR + "."))


def shell_errors(repo_root: Path) -> list[str]:
    """The shell directory may hold only the whitelisted files."""
    directory = repo_root / SHELL_DIR
    present = (
        {
            path.relative_to(repo_root).as_posix()
            for path in directory.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        if directory.is_dir()
        else set()
    )
    return [
        f"{name}: not a release-probe shell file; `shared/` holds only "
        f"{', '.join(sorted(SHELL_FILES))} (new code belongs in `base/`)"
        for name in sorted(present - SHELL_FILES)
    ]


def _docstrings(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _imported(node: ast.AST) -> list[str]:
    """Module names one node imports: statements and `import_module("...")` calls."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module] if node.level == 0 and node.module else []
    if not isinstance(node, ast.Call) or not node.args:
        return []
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    first = node.args[0]
    if name in _IMPORTERS and isinstance(first, ast.Constant) and isinstance(first.value, str):
        return [first.value]
    return []


def import_sites(tree: ast.Module) -> list[int]:
    """Lines that import the shell."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt | ast.expr)
        and any(_names_shell(module) for module in _imported(node))
    ]


def mention_sites(tree: ast.Module) -> list[tuple[int, str]]:
    """(line, mention) for every string literal that names the shell (docstrings aside)."""
    skip = _docstrings(tree)
    return [
        (node.lineno, match.group(0))
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
        for match in _MENTION.finditer(node.value)
    ]


def usage_errors(sources: Iterable[tuple[str, str]]) -> list[str]:
    """Imports anywhere, and string mentions outside tests beyond `ALLOWED_MENTIONS`.

    `sources` yields (repo-relative path, text) for every Python file to check.
    """
    errors: list[str] = []
    seen: Counter[tuple[str, str]] = Counter()
    scanned: set[str] = set()
    for rel, text in sources:
        scanned.add(rel)
        if SHELL_DIR not in text or rel in SHELL_FILES or rel in PINNED_ELSEWHERE:
            continue
        tree = ast.parse(text, filename=rel)
        errors += [
            f"{rel}:{line}: imports the `shared` release-probe shell; import `base` instead"
            for line in import_sites(tree)
        ]
        if _TEST_DIR.search(rel) or rel == _SELF:
            continue
        for line, mention in mention_sites(tree):
            key = (rel, mention)
            seen[key] += 1
            if key not in ALLOWED_MENTIONS:
                errors.append(
                    f"{rel}:{line}: names the `shared` release-probe shell (`{mention}`); only "
                    "the sites in scripts/structure/shared_shell.py ALLOWED_MENTIONS may"
                )
    for key, count in sorted(ALLOWED_MENTIONS.items()):
        rel, mention = key
        if rel in scanned and seen[key] != count:
            errors.append(
                f"{rel}: ALLOWED_MENTIONS expects `{mention}` {count}x, found {seen[key]}x - "
                "update the entry (or retire it with the shell)"
            )
    return errors


def repository_errors(repo_root: Path) -> list[str]:
    """Rule 7 over every tracked Python file (a checkout without git has no tracked set)."""
    listed = subprocess.run(  # noqa: S603 - fixed git query, script-derived root
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", "*.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode:
        print("note: shared-shell check skipped: not a git checkout", file=sys.stderr)
        return []
    sources = (
        (rel, text)
        for rel in listed.stdout.split("\0")
        if rel and (text := lint_common.read_utf8_text(repo_root / rel)) is not None
    )
    return shell_errors(repo_root) + usage_errors(sources)
