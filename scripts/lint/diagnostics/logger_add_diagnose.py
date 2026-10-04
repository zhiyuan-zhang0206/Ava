"""Forbid a `logger.add(...)` sink that does not explicitly pass `diagnose=False`.

Run: `.venv/bin/python scripts/lint/diagnostics/logger_add_diagnose.py [path ...]` (defaults
to scanning the whole repo; an explicit path that does not exist is an error
(stderr + exit 1) rather than a silent no-op). Also run automatically via
pre-commit hook.

Lives under `scripts/lint/` rather than directly under `scripts/`, alongside
`async_no_sync_blocking.py` — `scripts/` sits at its frozen 20+-entry
directory-budget ceiling (`scripts/structure/baseline/scripts.json`), so adding this
file directly there needed a same-PR relocation to net to zero. Not (yet) a
wholesale move of every `scripts/lint_*.py`.

## Why

loguru's `diagnose` defaults to `True`. With it on, `logger.exception(...)`
and `logger.opt(exception=exc)` render every local variable in the failing
traceback's frames into the log record. A frame that happens to hold a secret
— a DSN with an embedded password, a bearer token, a DB credential — puts that
secret in plaintext into whatever the sink writes: a log file, the systemd
journal, or the telemetry pipeline that mirrors into Loki. An independent
review reproduced this concretely: `psycopg.connect("postgresql://u:PASSWORD@...")`
failing and logged with `logger.exception` puts PASSWORD in the sink output.

No sink on `main` set `diagnose` explicitly, so every one of them defaulted to
`True`. This lint makes that the class of bug that cannot come back: every
`logger.add(...)` call outside test code must pass a literal `diagnose=False`.

`backtrace` is untouched by this lint — it controls whether the traceback
shows the extended chain of frames, not whether each frame's local variables
are rendered, so it carries none of this risk.

## The rule

Every call whose attribute is `.add(` on a `logger`-named object in any case
(`logger`, `_logger`, `self.logger`, `LOGGER`, ...) must pass a keyword
argument `diagnose` whose value is the literal `False`. Missing the keyword, passing a non-`False`
literal, or passing a value this script cannot verify statically (a name, a
`**kwargs` unpack) are all violations — an unverifiable pass would be an
unenforced rule wearing an enforced rule's clothes.

No inline exemption mechanism: an off-`diagnose` sink is not a legitimate
posture anywhere in this codebase, so there is nothing to opt out of.

Exempt: test code under `tests/`, `test_*.py`, `*_test.py` — pytest fixtures
that mount a throwaway loguru sink against a captured list have no secret in
their local frames to leak, and several such sinks in `tests/fixtures/log_capture.py` and
elsewhere predate this rule.

Error format `file:line: <message>` + non-zero exit.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import lint_common  # noqa: E402 - standalone script

# Scan directories — only OUR code, never .venv / node_modules / vendored trees.
_SCAN_DIRS = (*lint_common.FRAMEWORK_DIRS, "scripts")

_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)


def _is_test_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in _TEST_PATTERNS)


def _is_logger_add_call(func: ast.expr) -> bool:
    """True for `logger.add(...)` / `_logger.add(...)` / `self.logger.add(...)` —
    an attribute call named `add` on an object whose own name ends in `logger`,
    in any case (`LOGGER.add`, `runLogger.add`): the repo's convention is
    lowercase, but a name that breaks it is still a loguru sink."""
    if not isinstance(func, ast.Attribute) or func.attr != "add":
        return False
    base = func.value
    if isinstance(base, ast.Name):
        return base.id.lower().endswith("logger")
    if isinstance(base, ast.Attribute):
        return base.attr.lower().endswith("logger")
    return False


def _is_literal_false(value: ast.expr) -> bool:
    return isinstance(value, ast.Constant) and value.value is False


def violations_in_source(src: str, filename: str = "<source>") -> list[tuple[int, str]]:
    """Return [(lineno, message), ...] for `logger.add(...)` calls missing a
    literal `diagnose=False`.

    Takes source rather than a path so the lint's own tests can drive it with
    literal snippets (same shape as scripts/lint_pool_keepalives.py).
    """
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError as exc:
        return [(exc.lineno or 1, f"could not parse: {exc}")]

    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_logger_add_call(node.func):
            continue
        diagnose_kw = next((kw for kw in node.keywords if kw.arg == "diagnose"), None)
        if diagnose_kw is None:
            violations.append(
                (
                    node.lineno,
                    "logger.add(...) passes no `diagnose=False` — with loguru's "
                    "diagnose defaulting to True, logger.exception(...) on this sink "
                    "renders every local variable of the failing frames (DSNs, "
                    "tokens, passwords) into the sink's output",
                )
            )
        elif not _is_literal_false(diagnose_kw.value):
            violations.append(
                (
                    node.lineno,
                    "logger.add(..., diagnose=...) must pass the literal `False` — "
                    "a name or expression here cannot be verified statically and is "
                    "treated as unset",
                )
            )
    return violations


def _scan_file(path: Path, rel_path: str) -> list[tuple[int, str]]:
    """`violations_in_source` for one file, minus test files."""
    if _is_test_file(rel_path):
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []  # unreadable entry (e.g. a dangling symlink) or binary content
    return violations_in_source(text, str(path))


def _iter_py_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.append(root)
        elif root.is_dir():
            files.extend(root.rglob("*.py"))
    return files


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    if argv:
        missing = [arg for arg in argv if not Path(arg).exists()]
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    # argv non-empty = explicit paths; empty = full scan, or the `--only` changed files
    # (the commit hook) under the same scope.
    targets = (
        [Path(a).resolve() for a in argv]
        if argv
        else lint_common.scan_roots(_REPO_ROOT, _SCAN_DIRS)
    )
    scope = lint_common.changed_scope(only, _REPO_ROOT)

    total = 0
    for path in sorted(lint_common.restrict(_iter_py_files(targets), scope, _REPO_ROOT)):
        try:
            rel = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        for lineno, message in _scan_file(path, rel):
            total += 1
            print(f"{rel}:{lineno}: {message}")

    if total:
        print(
            f"\n{total} logger.add(...) call(s) without diagnose=False. See the "
            "docstring at the top of scripts/lint/diagnostics/logger_add_diagnose.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
