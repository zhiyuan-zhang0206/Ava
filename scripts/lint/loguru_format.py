"""Forbid printf-style placeholders in loguru log calls — their arguments are silently dropped.

Run: `.venv/bin/python scripts/lint/loguru_format.py [path ...]` (defaults to
the framework dirs plus `scripts/`; an explicit path that does not exist is an
error (stderr + exit 1) rather than a silent no-op). Also run automatically via
pre-commit hook.

## Why

`shared.log.logger` is loguru, and loguru formats a message with
`str.format(*args, **kwargs)`. A stdlib-style call

    logger.warning("gate for %s raised (failing open): %s", spec.session, exc)

has no `{}` field for `str.format` to fill, so it logs the literal text
`gate for %s raised (failing open): %s` and throws the service name and the
exception away. Nothing raises, nothing warns: the line that should explain a
failure reads as if it did, minus every detail. The stdlib `logging` module is
the opposite — `%s` is its placeholder — so the same line is correct there,
which is exactly how the habit slips into loguru code.

## The rule

A call `<logger>.<level>(message, *args, **kwargs)` — level one of `trace`,
`debug`, `info`, `success`, `warning`, `error`, `critical`, `exception`, or
`log(level, message, ...)` — on a loguru logger is flagged when `message` is a
string literal (or an f-string, checked on its literal parts) and either:

- it contains a printf conversion (`%s`, `%d`, `%r`, `%(name)s`, `%.2f`, ...;
  `%%` is not one) and the call passes format arguments, or
- the call passes positional format arguments but the message has no `{`
  field at all, so every one of them is dropped.

A loguru logger is a name bound by `from shared.log import logger` or
`from loguru import logger` (any alias), `loguru.logger` after `import loguru`,
or a name assigned from one of those through `.bind(...)` / `.opt(...)` /
`.patch(...)`; calls through a `.bind/.opt/.patch` chain are checked too. A
name that the module also binds any other way (a stdlib
`logging.getLogger(...)`, a parameter, a loop target) is ambiguous and is not
checked, so a stdlib logger — whose `%s` is correct — is never flagged.

## Exemption

`# log-format-ok: <reason>` on any line of the call, for a deliberate literal
`%` sequence (for example a test that proves the drop).

Error format `file:line: <message>` + non-zero exit.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scripts.structure import lint_common  # noqa: E402 - standalone script

_SCAN_DIRS = (*lint_common.FRAMEWORK_DIRS, "scripts")

_LOGURU_MODULES = frozenset({"shared.log", "loguru"})
_LEVEL_METHODS = frozenset(
    {"trace", "debug", "info", "success", "warning", "error", "critical", "exception"}
)
_CHAIN_METHODS = frozenset({"bind", "opt", "patch"})
_EXEMPT_MARKER = "# log-format-ok:"

# One printf conversion: %[(key)][flags][width][.precision]type. The space flag
# is left out on purpose so prose such as "100% done" is not read as `% d`.
_PRINTF_RE = re.compile(r"%(?:\([^)]*\))?[#0+\-]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[diouxXeEfFgGcrsa]")


def _bound_names(target: ast.expr) -> list[str]:
    """Plain names bound by an assignment / loop / with target (unpacking included)."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for elt in target.elts for name in _bound_names(elt)]
    return []


def _names_bound_by(node: ast.AST) -> list[str]:
    """Names a non-import node binds: targets, loop and `with` variables, walrus
    targets, parameters, and def / class names."""
    if isinstance(node, ast.Assign):
        return [name for target in node.targets for name in _bound_names(target)]
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor, ast.comprehension)):
        return _bound_names(node.target)
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return _bound_names(node.optional_vars)
    if isinstance(node, ast.NamedExpr):
        return [node.target.id]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    return []


def _from_import_bindings(node: ast.ImportFrom, loggers: set[str], other: set[str]) -> None:
    is_source = node.level == 0 and node.module in _LOGURU_MODULES
    for alias in node.names:
        bound = loggers if is_source and alias.name == "logger" else other
        bound.add(alias.asname or alias.name)


def _import_bindings(tree: ast.Module) -> tuple[set[str], set[str], bool]:
    """(names imported as the loguru logger, names imported as anything else,
    whether the bare `loguru` module is imported)."""
    loggers: set[str] = set()
    other: set[str] = set()
    loguru_module = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            _from_import_bindings(node, loggers, other)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "loguru" and alias.asname is None:
                    loguru_module = True
                else:
                    other.add(alias.asname or alias.name.partition(".")[0])
    return loggers, other, loguru_module


class _Bindings:
    """Which names in one module are loguru loggers."""

    def __init__(self, tree: ast.Module) -> None:
        self.names, other, self.loguru_module = _import_bindings(tree)
        derivations = self._add_derived_loggers(tree)
        for node in ast.walk(tree):
            if id(node) not in derivations:
                other.update(_names_bound_by(node))
        self.names -= other

    def _add_derived_loggers(self, tree: ast.Module) -> set[int]:
        """Add names assigned from a loguru logger (`log = logger.bind(...)`), to a
        fixed point; return the ids of those deriving assignments."""
        assigns = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))]
        derivations: set[int] = set()
        grew = True
        while grew:
            before = len(self.names)
            for node in assigns:
                if node.value is not None and self.is_loguru(node.value):
                    derivations.add(id(node))
                    self.names.update(_names_bound_by(node))
            grew = len(self.names) > before
        return derivations

    def is_loguru(self, expr: ast.expr) -> bool:
        if isinstance(expr, ast.Name):
            return expr.id in self.names
        if isinstance(expr, ast.Attribute):
            return (
                self.loguru_module
                and expr.attr == "logger"
                and isinstance(expr.value, ast.Name)
                and expr.value.id == "loguru"
            )
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
            return expr.func.attr in _CHAIN_METHODS and self.is_loguru(expr.func.value)
        return False


def _literal_text(node: ast.expr) -> str | None:
    """The literal text of a str constant, or the literal parts of an f-string."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return None


def _message_index(node: ast.Call, bindings: _Bindings) -> int | None:
    """Position of the message argument of a loguru log call, or None."""
    func = node.func
    if not isinstance(func, ast.Attribute) or not bindings.is_loguru(func.value):
        return None
    index = 0 if func.attr in _LEVEL_METHODS else 1 if func.attr == "log" else None
    return index if index is not None and len(node.args) > index else None


def _call_problem(node: ast.Call, bindings: _Bindings) -> str | None:
    index = _message_index(node, bindings)
    text = None if index is None else _literal_text(node.args[index])
    if index is None or text is None:
        return None
    positional = node.args[index + 1 :]
    printf = _PRINTF_RE.search(text.replace("%%", ""))
    if printf is not None and (positional or node.keywords):
        return (
            f"loguru call uses the printf placeholder {printf.group(0)!r}; loguru formats "
            "with str.format, so the arguments are silently dropped — use `{}` fields"
        )
    if positional and "{" not in text:
        return (
            "loguru call passes positional arguments but the message has no `{}` field, "
            "so the arguments are silently dropped"
        )
    return None


def _is_exempt(node: ast.Call, lines: list[str]) -> bool:
    call_lines = lines[node.lineno - 1 : node.end_lineno or node.lineno]
    return any(_EXEMPT_MARKER in line for line in call_lines)


def violations_in_source(src: str, filename: str = "<source>") -> list[tuple[int, str]]:
    """Return [(lineno, message), ...] for loguru calls that drop their arguments.

    Takes source rather than a path so the lint's own tests can drive it with
    literal snippets.
    """
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError as exc:
        return [(exc.lineno or 1, f"could not parse: {exc}")]
    bindings = _Bindings(tree)
    lines = src.splitlines()
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            problem = _call_problem(node, bindings)
            if problem is not None and not _is_exempt(node, lines):
                out.append((node.lineno, problem))
    return sorted(out)


def _default_files() -> list[str]:
    roots = lint_common.scan_roots(_REPO_ROOT, _SCAN_DIRS)
    return sorted(p.relative_to(_REPO_ROOT).as_posix() for r in roots for p in r.rglob("*.py"))


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv:
        files, missing = lint_common.resolve_targets(argv, _REPO_ROOT)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    else:
        files = _default_files()

    total = 0
    for rel in files:
        text = lint_common.read_utf8_text(_REPO_ROOT / rel) if rel.endswith(".py") else None
        for lineno, message in violations_in_source(text, rel) if text is not None else ():
            total += 1
            print(f"{rel}:{lineno}: {message}")

    if total:
        print(
            f"\n{total} loguru call(s) that drop their arguments. Use `{{}}` fields; see "
            "the docstring at the top of scripts/lint/loguru_format.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
