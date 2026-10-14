"""Python interpreters started as a subprocess: the one thing they may run is a file or module the checker can see.

A command whose first element is the current interpreter (`sys.executable`), a literal path ending in
`python`/`python3`, or a name bound to one of those, may continue with interpreter flags and then
exactly one of:

- `-m <module>` with a literal module name: for a first-party module an import-like fact edge to its file;
- a literal path of a tracked file (`"scripts/x.py"`, `Path(__file__).with_name("x.py")`,
  `Path(__file__).parent / "x.py"`, `.parents[n]`, `.resolve()`, `str(...)`, or a name bound once to
  such an expression): an edge to that file. A path that resolves to nothing tracked is an error.

Anything else cannot be seen by the checker and is a restricted form: `-c` whatever follows, a path
that is not literal (a temporary file), source on standard input, or arguments built with `*`.
"""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from meizel.graph.model import Code, FactKind
from meizel.language.restricted import RestrictedForms, const_str

__all__ = ["PythonCommands"]

_INTERPRETER = re.compile(r"python(3(\.\d+)?)?")
_FLAGS_WITH_VALUE = frozenset({"-X", "-W"})
_ROOT = PurePosixPath(".")


class PythonCommands(RestrictedForms):
    """Reads argument lists that start a Python interpreter."""

    def interpreter_command(
        self, elements: list[ast.expr], line: int, *, bare: bool | None = None
    ) -> None:
        """Judge an argument list that starts a Python interpreter.

        A bare `python`/`python3` is a command only where a process is spawned: the same two
        strings open tuples that are table data or the description of a process under test. So a
        list literal met anywhere is judged only when it starts with `sys.executable` or a path
        (`bare=False`), and the first argument of a spawning call only when it starts with a bare
        name (`bare=True`); `None` judges both, for arguments that are not a list.
        """
        if not elements or not self.is_interpreter(elements[0]):
            return
        if bare is not None and self.is_bare_name(elements[0]) != bare:
            return
        index = 1
        while index < len(elements):
            element = elements[index]
            if isinstance(element, ast.Starred):
                self.restrict(line, "python subprocess with arguments built with *")
                return
            flag = const_str(element)
            if flag == "-c":
                self.restrict(line, "python -c")
                return
            if flag == "-":
                self.restrict(line, "python source on standard input")
                return
            if flag == "-m":
                self.module_target(elements[index + 1] if index + 1 < len(elements) else None, line)
                return
            if flag is not None and flag.startswith("-"):
                index += 2 if flag in _FLAGS_WITH_VALUE else 1
                continue
            self.file_target(element, line)
            return

    def is_bare_name(self, expr: ast.expr) -> bool:
        """A literal `python` / `python3` with no directory, the only form that is not `sys.executable`."""
        text = const_str(expr)
        return text is not None and "/" not in text and self.is_interpreter(expr)

    def is_interpreter(self, expr: ast.expr, depth: int = 0) -> bool:
        """`sys.executable`, a literal `.../python[3[.x]]`, or a name bound once to one of those."""
        if depth > 4:
            return False
        if self.dotted(expr) == "sys.executable":
            return True
        text = const_str(expr)
        if text is not None:
            return _INTERPRETER.fullmatch(text.rsplit("/", 1)[-1]) is not None
        if isinstance(expr, ast.Name):
            value = self.scope.constant(expr.id)
            return value is not None and self.is_interpreter(value, depth + 1)
        return False

    def module_target(self, name: ast.expr | None, line: int) -> None:
        text = None if name is None else self.string_value(name)
        if text is None or "{" in text:
            self.restrict(line, "python -m of a module that is not a literal")
            return
        if not self.u.is_first_party_name(text):
            return
        module = self.u.resolve_module(text)
        file = None if module is None else module.file
        if file is None:
            self.problem(
                line, f"`python -m {text}` names no tracked module", Code.UNRESOLVED_IMPORT
            )
            return
        self.add(file, line, FactKind.IMPORT, (), f"python -m {text}")
        entry = file.removesuffix("__init__.py") + "__main__.py"
        if file.endswith("__init__.py") and entry in self.u.files:  # a package runs its __main__
            self.add(entry, line, FactKind.IMPORT, (), f"python -m {text}")

    def file_target(self, expr: ast.expr, line: int) -> None:
        path = self.literal_path(expr)
        if path is None:
            self.restrict(
                line, "python subprocess that runs something other than a literal repository file"
            )
            return
        if path.as_posix() not in self.u.files:
            self.problem(line, f"`python {path}` names no tracked file", Code.UNRESOLVED_IMPORT)
            return
        if path.as_posix() != self.path:  # a file that starts itself again depends on nothing new
            self.add(path.as_posix(), line, FactKind.IMPORT, (), f"python {path}")

    def literal_path(self, expr: ast.expr, depth: int = 0) -> PurePosixPath | None:
        """The repository-relative path an expression names without running anything, or None."""
        if depth > 40:
            return None
        text = const_str(expr)
        if text is not None:
            return None if text.startswith(("/", "~")) or not text else PurePosixPath(text)
        if isinstance(expr, ast.Name):
            return self.name_path(expr, depth)
        if isinstance(expr, ast.Call):
            return self.path_call(expr, depth)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
            left, right = self.literal_path(expr.left, depth + 1), const_str(expr.right)
            return None if left is None or right is None else left / right
        if isinstance(expr, ast.Attribute):
            return self.attribute_path(expr, depth)
        if isinstance(expr, ast.Subscript):
            return self.ancestor(expr, depth)
        return None

    def name_path(self, name: ast.Name, depth: int) -> PurePosixPath | None:
        if name.id == "__file__" and self.scope.find("__file__") == (None, False):
            return PurePosixPath(self.path)
        value = self.scope.constant(name.id)
        return None if value is None else self.literal_path(value, depth + 1)

    def attribute_path(self, expr: ast.Attribute, depth: int) -> PurePosixPath | None:
        if expr.attr == "__file__":
            module = self.module_file(expr.value)  # `<first-party module>.__file__`
            return None if module is None else PurePosixPath(module)
        if expr.attr == "parent":
            owner = self.literal_path(expr.value, depth + 1)
            return None if owner is None else owner.parent
        return None

    def path_call(self, call: ast.Call, depth: int) -> PurePosixPath | None:
        func = call.func
        if isinstance(func, ast.Name) and func.id == "str" and len(call.args) == 1:
            return self.literal_path(call.args[0], depth + 1)
        if self.dotted(func) in {"pathlib.Path", "pathlib.PurePath"}:
            return self.literal_path(call.args[0], depth + 1) if len(call.args) == 1 else None
        if not isinstance(func, ast.Attribute):
            return None
        owner = self.literal_path(func.value, depth + 1)
        if owner is None:
            return None
        if func.attr in {"resolve", "absolute"} and not call.args:
            return owner
        name = const_str(call.args[0]) if len(call.args) == 1 else None
        return owner.with_name(name) if func.attr == "with_name" and name and owner.name else None

    def ancestor(self, expr: ast.Subscript, depth: int) -> PurePosixPath | None:
        """`<path>.parents[n]`: the n-th parent, where `parents[0]` is `parent`."""
        owner, step = expr.value, expr.slice
        if not (isinstance(owner, ast.Attribute) and owner.attr == "parents"):
            return None
        if not (isinstance(step, ast.Constant) and isinstance(step.value, int) and step.value >= 0):
            return None
        base = self.literal_path(owner.value, depth + 1)
        parents = [] if base is None else list(base.parents)
        return parents[step.value] if step.value < len(parents) else None
