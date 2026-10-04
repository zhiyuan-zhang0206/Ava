"""Forbid log calls whose message format does not match their logger's formatter —
and the `exc_info` kwarg loguru silently ignores.

Two mirror-image format rules, one per formatter, plus one lost-cause rule:
loguru has no `exc_info` parameter (stdlib logging does), so
`logger.warning(..., exc_info=True)` rides the record's `extra` and the
traceback is never attached; `logger.opt(exception=True)` is the way it goes on
the call. Run:
`.venv/bin/python scripts/lint/loguru_format.py [path ...]` (defaults to
the framework dirs plus `scripts/`; an explicit path that does not exist is an
error (stderr + exit 1) rather than a silent no-op). Also run automatically via
pre-commit hook.

## Why

`base.log.logger` is loguru, and loguru formats a message with
`str.format(*args, **kwargs)`. A stdlib-style call

    logger.warning("gate for %s raised (failing open): %s", spec.session, exc)

has no `{}` field for `str.format` to fill, so it logs the literal text
`gate for %s raised (failing open): %s` and throws the service name and the
exception away. Nothing raises, nothing warns: the line that should explain a
failure reads as if it did, minus every detail. The stdlib `logging` module is
the opposite — `%s` is its placeholder — so the same line is correct there,
which is exactly how the habit slips into loguru code.

The habit slips the other way too. A loguru-style call on a stdlib logger,

    _log.info("flush pass: delivered={} expired={}", report.delivered, report.expired)

leaves `{}` in the text and hands `msg % args` two arguments it cannot consume:
`TypeError: not all arguments converted during string formatting`, raised while
the record is formatted. In production the root handler swallows that into a
"Logging error" traceback on stderr, so the line — a dead-letter redelivery
summary, a scheduler refusal — never reaches the log file or the event stream;
under pytest the capture handler re-raises it, which is how it stayed hidden
until a test happened to run with the root logger at INFO.

A third habit slips through just as silently. loguru has no `exc_info`
parameter (stdlib `logging` does), so

    logger.warning("gate raised; failing open", exc_info=True)

logs the message, moves `exc_info` into the record's `extra`, and attaches
nothing to `record["exception"]` — the line reads complete while its traceback
is gone.

## Rule 1: loguru loggers

A call `<logger>.<level>(message, *args, **kwargs)` — level one of `trace`,
`debug`, `info`, `success`, `warning`, `error`, `critical`, `exception`, or
`log(level, message, ...)` — on a loguru logger is flagged when `message` is a
string literal (or an f-string, checked on its literal parts) and either:

- it contains a printf conversion (`%s`, `%d`, `%r`, `%(name)s`, `%.2f`, ...;
  `%%` is not one) and the call passes format arguments, or
- the call passes positional format arguments but the message has no `{`
  field at all, so every one of them is dropped.

A loguru logger is a name bound by `from base.log import logger` or
`from loguru import logger` (any alias), `loguru.logger` after `import loguru`,
or a name assigned from one of those through `.bind(...)` / `.opt(...)` /
`.patch(...)`; calls through a `.bind/.opt/.patch` chain are checked too. A
name that the module also binds any other way (a stdlib
`logging.getLogger(...)`, a parameter, a loop target) is ambiguous and is not
checked by this rule.

## Rule 2: stdlib loggers

A call `<logger>.<level>(message, *args)` — level one of `debug`, `info`,
`warning`, `warn`, `error`, `critical`, `fatal`, `exception`, or
`log(level, message, ...)` — on a stdlib logger is flagged when `message` is a
string literal (or an f-string, checked on its literal parts) that has a `{}`
field (`{}`, `{0}`, `{name}`, `{:.2f}`; `{{` is not one) and no printf
conversion, and the call passes positional arguments. Keyword arguments
(`exc_info=`, `extra=`) are not format arguments and do not count.

A stdlib logger is a name every binding of which, in the module, is an
assignment from `logging.getLogger(...)` (or a `from logging import getLogger`
alias, or `.getChild(...)` of a stdlib logger), the call expression itself
(`logging.getLogger(__name__).warning(...)`), or the root-logger shortcut
`logging.info(...)`. The distinction is by variable, not by file, so a module
that holds both a loguru `logger` and a stdlib `_log` is checked correctly on
both; a name the module also binds any other way (a `from base.log import
logger as _log`, a parameter, a loop target) is ambiguous and is not checked.

## Rule 3: the `exc_info` kwarg on loguru

A loguru log call passing `exc_info` in any form (`exc_info=True`,
`exc_info=exc`) is flagged: loguru has no such parameter, the kwarg rides the
record's `extra`, and the traceback is lost — use `logger.opt(exception=True)`
(or `logger.opt(exception=exc)` when an exception object is in hand; task
#4979). stdlib loggers keep the kwarg: `exc_info` is the correct parameter
there.

## Exemption

`# log-format-ok: <reason>` on any line of the call, for a deliberate literal
`%` or `{}` sequence (for example a test that proves the drop).

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

_LOGURU_MODULES = frozenset({"base.log", "loguru"})
_LEVEL_METHODS = frozenset(
    {"trace", "debug", "info", "success", "warning", "error", "critical", "exception"}
)
_CHAIN_METHODS = frozenset({"bind", "opt", "patch"})
# stdlib `logging.Logger` has no trace/success; it does have the `warn` / `fatal` aliases.
_STDLIB_LEVEL_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "critical", "fatal", "exception"}
)
_EXEMPT_MARKER = "# log-format-ok:"

# One printf conversion: %[(key)][flags][width][.precision]type. The space flag
# is left out on purpose so prose such as "100% done" is not read as `% d`.
_PRINTF_RE = re.compile(r"%(?:\([^)]*\))?[#0+\-]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[diouxXeEfFgGcrsa]")

# One `str.format` replacement field once the `{{` / `}}` escapes are removed.
_BRACE_FIELD_RE = re.compile(r"\{[^{}]*\}")


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


class _StdlibBindings:
    """Which names in one module are stdlib loggers, and which names are the `logging` module."""

    def __init__(self, tree: ast.Module) -> None:
        self.modules: set[str] = set()  # `import logging [as lg]`
        self.factories: set[str] = set()  # `from logging import getLogger [as g]`
        imported = self._scan_imports(tree)
        self.names: set[str] = set()
        derivations = self._add_derived_loggers(tree)
        other = set(imported)
        for node in ast.walk(tree):
            if id(node) not in derivations:
                other.update(_names_bound_by(node))
        self.names -= other

    def _scan_imports(self, tree: ast.Module) -> set[str]:
        """Record the `logging` aliases and return every name any import binds."""
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.asname or alias.name.partition(".")[0])
                    self._note_module_alias(alias)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.asname or alias.name)
                    if node.level == 0 and node.module == "logging" and alias.name == "getLogger":
                        self.factories.add(alias.asname or alias.name)
        return imported

    def _note_module_alias(self, alias: ast.alias) -> None:
        """`import logging [as lg]` and `import logging.handlers` bind the `logging` module."""
        if alias.name == "logging":
            self.modules.add(alias.asname or "logging")
        elif alias.asname is None and alias.name.startswith("logging."):
            self.modules.add("logging")

    def _add_derived_loggers(self, tree: ast.Module) -> set[int]:
        """Add names assigned from a stdlib logger (`_log = logging.getLogger(...)`), to a
        fixed point; return the ids of those deriving assignments."""
        assigns = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))]
        derivations: set[int] = set()
        grew = True
        while grew:
            before = len(self.names)
            for node in assigns:
                if node.value is not None and self.is_stdlib(node.value):
                    derivations.add(id(node))
                    self.names.update(_names_bound_by(node))
            grew = len(self.names) > before
        return derivations

    def is_stdlib(self, expr: ast.expr) -> bool:
        """A stdlib logger, or a `logging.getLogger(...)` / `<logger>.getChild(...)` call."""
        if isinstance(expr, ast.Name):
            return expr.id in self.names
        if not isinstance(expr, ast.Call):
            return False
        func = expr.func
        if isinstance(func, ast.Name):
            return func.id in self.factories
        if isinstance(func, ast.Attribute):
            if func.attr == "getLogger":
                return isinstance(func.value, ast.Name) and func.value.id in self.modules
            return func.attr == "getChild" and self.is_stdlib(func.value)
        return False

    def is_logging_module(self, expr: ast.expr) -> bool:
        return isinstance(expr, ast.Name) and expr.id in self.modules


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


def _exc_info_problem(node: ast.Call, bindings: _Bindings) -> str | None:
    """A loguru log call passing `exc_info` — a stdlib-only parameter it ignores."""
    func = node.func
    if not isinstance(func, ast.Attribute) or not bindings.is_loguru(func.value):
        return None
    if func.attr not in _LEVEL_METHODS and func.attr != "log":
        return None
    if not any(keyword.arg == "exc_info" for keyword in node.keywords):
        return None
    return (
        "loguru call passes `exc_info`, which loguru has no parameter for — the kwarg rides "
        "the record's `extra` and the traceback is never attached; use "
        "`logger.opt(exception=True)`"
    )


def _stdlib_message_index(node: ast.Call, bindings: _StdlibBindings) -> int | None:
    """Position of the message argument of a stdlib log call, or None. The receiver is a
    stdlib logger or the `logging` module itself (the root-logger shortcuts)."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if not (bindings.is_stdlib(func.value) or bindings.is_logging_module(func.value)):
        return None
    index = 0 if func.attr in _STDLIB_LEVEL_METHODS else 1 if func.attr == "log" else None
    return index if index is not None and len(node.args) > index else None


def _stdlib_call_problem(node: ast.Call, bindings: _StdlibBindings) -> str | None:
    index = _stdlib_message_index(node, bindings)
    text = None if index is None else _literal_text(node.args[index])
    if index is None or text is None or len(node.args) <= index + 1:
        return None  # not a stdlib call with a literal message and positional arguments
    if _PRINTF_RE.search(text.replace("%%", "")):
        return None  # a printf conversion consumes the arguments
    field = _BRACE_FIELD_RE.search(text.replace("{{", "").replace("}}", ""))
    if field is None:
        return None
    return (
        f"stdlib logging call uses the `str.format` field {field.group(0)!r} with positional "
        "arguments; logging formats with `%`, so the field is left in the text and the "
        "arguments raise TypeError at emit — use `%s` placeholders"
    )


def _is_exempt(node: ast.Call, lines: list[str]) -> bool:
    call_lines = lines[node.lineno - 1 : node.end_lineno or node.lineno]
    return any(_EXEMPT_MARKER in line for line in call_lines)


def violations_in_source(src: str, filename: str = "<source>") -> list[tuple[int, str]]:
    """Return [(lineno, message), ...] for log calls whose message format does not match
    their logger: loguru calls that drop their arguments, stdlib calls that cannot
    consume them.

    Takes source rather than a path so the lint's own tests can drive it with
    literal snippets.
    """
    try:
        tree = ast.parse(src, filename=filename)
    except SyntaxError as exc:
        return [(exc.lineno or 1, f"could not parse: {exc}")]
    loguru = _Bindings(tree)
    stdlib = _StdlibBindings(tree)
    lines = src.splitlines()
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            problem = (
                _call_problem(node, loguru)
                or _exc_info_problem(node, loguru)
                or _stdlib_call_problem(node, stdlib)
            )
            if problem is not None and not _is_exempt(node, lines):
                out.append((node.lineno, problem))
    return sorted(out)


def _default_files() -> list[str]:
    roots = lint_common.scan_roots(_REPO_ROOT, _SCAN_DIRS)
    return sorted(p.relative_to(_REPO_ROOT).as_posix() for r in roots for p in r.rglob("*.py"))


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    if argv:
        files, missing = lint_common.resolve_targets(argv, _REPO_ROOT)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    else:
        # The default scope, or only the `--only` changed files (the commit hook) inside it.
        scope = lint_common.changed_scope(only, _REPO_ROOT)
        files = _default_files() if scope is None else [f for f in _default_files() if f in scope]

    total = 0
    for rel in files:
        text = lint_common.read_utf8_text(_REPO_ROOT / rel) if rel.endswith(".py") else None
        for lineno, message in violations_in_source(text, rel) if text is not None else ():
            total += 1
            print(f"{rel}:{lineno}: {message}")

    if total:
        print(
            f"\n{total} log call(s) that lose their message or traceback: loguru takes "
            "`{}` fields and `logger.opt(exception=True)` (it has no `exc_info`); stdlib "
            "logging takes `%s`; see the docstring at the top of scripts/lint/loguru_format.py.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
