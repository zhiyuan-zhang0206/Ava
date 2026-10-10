"""Every patch point in a test file, resolved to a target when it can be.

A patch point is one attribute, dict key or environment variable a test replaces:

- string form: `monkeypatch.setattr("a.b.c", v)` / `delattr`, `patch("a.b.c")`,
  `mocker.patch(...)`, `patch.dict("a.b.DICT", ...)`, `patch.multiple("a.b", x=..., y=...)`;
- object form: `monkeypatch.setattr(obj, "attr", v)` / `delattr`, `patch.object(obj, "attr")`,
  `setitem(obj, key, v)`, `patch.dict(obj, ...)`;
- environment: `monkeypatch.setenv` / `delenv`, `patch.dict(os.environ, {...})`,
  `setitem(os.environ, ...)`;
- ambient: `monkeypatch.chdir` / `syspath_prepend`.

Decorator and `with` uses are the same calls. `obj` is resolved through the file's imports
and simple `x = <imported thing>` / `x = Klass(...)` aliases (two passes, no data flow);
anything else (parameters, `self.x`, call results) is `unresolved` and only counted.
"""

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from scripts.structure import imports
from scripts.structure.placement import CODE_TOPS

__all__ = [
    "Point",
    "extract_points",
]

_STDLIB = frozenset(sys.stdlib_module_names)
_KNOWN_THIRD_PARTY = frozenset(
    {
        "requests",
        "httpx",
        "psutil",
        "yaml",
        "redis",
        "psycopg",
        "urllib",
        "opentelemetry",
        "langgraph",
        "langchain_core",
        "pytest",
        "aiohttp",
        "pandas",
        "numpy",
    }
)
_DOTTED = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")
_NOT_MONKEYPATCH = frozenset({"self", "cls", "os", "sys", "object", "builtins", "super"})
_MONKEYPATCH_CALLS = frozenset(
    {"setattr", "delattr", "setitem", "delitem", "setenv", "delenv", "chdir", "syspath_prepend"}
)
_MOCK_PLAIN = frozenset({"patch", "mock.patch", "mocker.patch", "unittest.mock.patch"})
_MOCK_FORMS = (".patch.object", ".patch.dict", ".patch.multiple")
_MOCK_CONTROL_KWARGS = frozenset({"spec", "create", "spec_set", "autospec", "new_callable"})
_LOADER_ROOTS = frozenset({"str", "Path", "fspath"})
_LOADER_METHODS = frozenset({"resolve", "expanduser", "absolute"})
_MAX_PATH_DEPTH = 3


@dataclass(frozen=True)
class Point:
    """One patch point. `dotted` is the resolved target; `unresolved` marks an unknown object."""

    line: int
    form: str  # string | object | env | ambient
    via: str
    dotted: str | None = None
    expr: str = ""
    attr: str | None = None
    env: str | None = None
    unresolved: bool = False


def _callee(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _callee(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _chain(node: ast.AST) -> tuple[str | None, list[str]]:
    """`a.b.c` -> ("a", ["b", "c"]); (None, []) when the root is not a plain Name."""
    attrs: list[str] = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    return (node.id, attrs[::-1]) if isinstance(node, ast.Name) else (None, [])


def _const_chain(node: ast.AST) -> list[str | None]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _const_chain(node.left) + _const_chain(node.right)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    return [None]


def _string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _literal_run(parts: list[str | None]) -> str:
    """The leading run of literal operands of a `/` chain, joined."""
    chunk: list[str] = []
    for piece in parts:
        if piece is None:
            break
        chunk.append(piece.strip("/"))
    return "/".join(chunk)


def _joinpath_literal(node: ast.AST) -> str | None:
    """`base.joinpath("scripts", "x")` with literal parts naming a code top."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "joinpath"
    ):
        return None
    consts = [
        a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
    ]
    return "/".join(consts) if consts and consts[0] in CODE_TOPS else None


class _Names:
    """Local name -> (dotted origin, loaded_by_path) for one file."""

    def __init__(self, nodes: Sequence[ast.AST], rel_path: str) -> None:
        self.names: dict[str, tuple[str, bool]] = {}
        self.values: dict[str, ast.expr] = {}
        for node in nodes:
            self._bind(node, rel_path)
        self.specs = {
            name: module for name, value in self.values.items() if (module := self._spec(value))
        }
        for _ in range(2):
            for name, value in list(self.values.items()):
                if name not in self.names and name not in self.specs:
                    self._alias(name, value)

    def _bind(self, node: ast.AST, rel_path: str) -> None:
        if isinstance(node, ast.Import | ast.ImportFrom):
            clause = imports.normalize(node, rel_path)
            self.names.update((name, (origin, False)) for name, origin in clause.origins.items())
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) == 1 and isinstance(targets[0], ast.Name) and node.value is not None:
                self.values.setdefault(targets[0].id, node.value)

    def _alias(self, name: str, value: ast.expr) -> None:
        """Bind `name = <imported thing>` / `name = Klass(...)` / a path-loaded module."""
        if isinstance(value, ast.Call):
            loaded = self._loaded_module(value)
            if loaded is not None:
                self.names[name] = loaded
                return
            value = value.func  # an instance: patching it patches its class's module
        if isinstance(value, ast.Await):
            return
        origin = self.resolve(value)
        if origin:
            self.names[name] = origin

    def _loaded_module(self, call: ast.Call) -> tuple[str, bool] | None:
        function = self.resolve(call.func)
        function_name = function[0] if function else ""
        first = call.args[0] if call.args else None
        text = _string(first)
        if function_name in {"importlib.import_module", "pytest.importorskip"} and text:
            return text, False
        if function_name.endswith("load_skill_script"):
            parts = [
                c.value
                for c in call.args
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            ]
            return self._path_module("ava_builtins/skills/" + "/".join(parts)), True
        if function_name == "importlib.util.module_from_spec" and first is not None:
            module = self._spec(first) or (
                self.specs.get(first.id) if isinstance(first, ast.Name) else None
            )
            return (module, True) if module else None
        return None

    def _path_expr(self, node: ast.AST, depth: int = 0) -> str | None:
        """The repo-relative path a `spec_from_file_location` argument names, if literal."""
        if depth > _MAX_PATH_DEPTH:
            return None
        inner = self._path_through(node)
        if inner is not None:
            return self._path_expr(inner, depth + 1)
        parts = _const_chain(node)
        for position, part in enumerate(parts):
            if part and part.split("/")[0] in CODE_TOPS:
                return _literal_run(parts[position:])
        return _joinpath_literal(node)

    def _path_through(self, node: ast.AST) -> ast.AST | None:
        """The operand a wrapper (`str(x)`, `x.resolve()`) or a local name stands for."""
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id in _LOADER_ROOTS and node.args:
                return node.args[0]
            if isinstance(function, ast.Attribute) and function.attr in _LOADER_METHODS:
                return function.value
        if isinstance(node, ast.Name) and node.id in self.values and node.id != "__file__":
            return self.values[node.id]
        return None

    @staticmethod
    def _path_module(rel: str) -> str:
        parts = rel.strip("/").split("/")
        if parts[-1].endswith(".py"):
            parts[-1] = parts[-1][:-3]
        keep: list[str] = []
        for segment in parts:
            if not re.fullmatch(r"[A-Za-z_]\w*", segment):
                break
            keep.append(segment)
        return ".".join(keep)

    def _spec(self, node: ast.AST) -> str | None:
        if (
            isinstance(node, ast.Call)
            and _callee(node.func).endswith("spec_from_file_location")
            and len(node.args) >= 2
        ):
            rel = self._path_expr(node.args[1])
            if rel:
                return self._path_module(rel)
        return None

    def resolve(self, node: ast.AST) -> tuple[str, bool] | None:
        """(dotted origin of the expression, loaded_by_path), or None if unknown."""
        root, chain = _chain(node)
        if root is None or root not in self.names:
            return None
        dotted, loaded = self.names[root]
        if (
            loaded
            and chain
            and (chain[0] in _STDLIB or chain[0] in CODE_TOPS or chain[0] in _KNOWN_THIRD_PARTY)
        ):
            return ".".join(
                chain
            ), False  # `script_module.subprocess.run` patches subprocess itself
        return ".".join([dotted, *chain]) if chain else dotted, loaded


class _Extractor:
    def __init__(self, nodes: Sequence[ast.AST], rel_path: str) -> None:
        self.names = _Names(nodes, rel_path)
        self.points: list[Point] = []

    def _add(
        self,
        line: int,
        form: str,
        via: str,
        *,
        dotted: str | None = None,
        expr: str = "",
        attr: str | None = None,
        env: str | None = None,
        unresolved: bool = False,
    ) -> None:
        self.points.append(Point(line, form, via, dotted, expr, attr, env, unresolved))

    def _object(self, line: int, via: str, obj: ast.AST, attr: str | None) -> None:
        text = ast.unparse(obj)
        if text == "os.environ" or text.endswith(".environ"):
            self._add(line, "env", via, expr=text, env=attr)
            return
        origin = self.names.resolve(obj)
        if origin is None:
            self._add(line, "object", via, expr=text[:90], attr=attr, unresolved=True)
        else:
            dotted = origin[0] + (f".{attr}" if attr else "")
            self._add(line, "object", via, dotted=dotted, expr=text[:90], attr=attr)

    def monkeypatch(self, call: ast.Call, name: str, last: str) -> None:
        args, line = call.args, call.lineno
        if last in {"setenv", "delenv"}:
            self._add(
                line, "env", name, env=_string(args[0]) if args else None, expr="monkeypatch.env"
            )
        elif last in {"chdir", "syspath_prepend"}:
            self._add(line, "ambient", name, expr=last)
        elif last in {"setattr", "delattr"} and args:
            self._setattr(line, name, args)
        elif last in {"setitem", "delitem"} and args:
            self._object(line, name, args[0], _string(args[1]) if len(args) > 1 else None)

    def _setattr(self, line: int, name: str, args: list[ast.expr]) -> None:
        target = _string(args[0])
        if target is not None:
            if _DOTTED.match(target):
                self._add(line, "string", name, dotted=target, expr=target)
        elif len(args) >= 2:
            self._object(line, name, args[0], _string(args[1]))

    def mock(self, call: ast.Call, name: str) -> None:
        args, line = call.args, call.lineno
        keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
        sub = "" if name in _MOCK_PLAIN else name.rsplit(".", 1)[-1]
        if sub == "":
            self._patch_string(line, name, args, keywords)
        elif sub == "object" and args:
            self._object(line, name, args[0], _string(args[1]) if len(args) >= 2 else None)
        elif sub == "dict" and args:
            self._patch_dict(line, name, args)
        elif sub == "multiple" and args:
            self._patch_multiple(line, name, args[0], keywords)

    def _patch_string(
        self, line: int, name: str, args: list[ast.expr], keywords: dict[str, ast.expr]
    ) -> None:
        target = _string(args[0]) if args else None
        if target is not None and _DOTTED.match(target):
            self._add(line, "string", name, dotted=target, expr=target)
        elif not args and _string(keywords.get("target")) is not None:
            target = _string(keywords["target"]) or ""
            self._add(line, "string", name, dotted=target, expr=target)

    def _patch_dict(self, line: int, name: str, args: list[ast.expr]) -> None:
        first = args[0]
        target = _string(first)
        text = target if target is not None else ast.unparse(first)
        if text == "os.environ" or (target is None and text.endswith("environ")):
            keys = (
                [_string(k) for k in args[1].keys if k is not None]
                if len(args) > 1 and isinstance(args[1], ast.Dict)
                else []
            )
            for key in keys or [None]:
                self._add(line, "env", name, env=key, expr=text)
        elif target is not None:
            if _DOTTED.match(target):
                self._add(line, "string", name, dotted=target, expr=target)
        else:
            self._object(line, name, first, None)

    def _patch_multiple(
        self, line: int, name: str, first: ast.expr, keywords: dict[str, ast.expr]
    ) -> None:
        attrs = [key for key in keywords if key not in _MOCK_CONTROL_KWARGS]
        target = _string(first)
        for attr in attrs or [None]:
            if target is not None and _DOTTED.match(target):
                self._add(
                    line, "string", name, dotted=target + (f".{attr}" if attr else ""), expr=target
                )
            else:
                self._object(line, name, first, attr)


def extract_points(nodes: Sequence[ast.AST], rel_path: str = "") -> list[Point]:
    """Patch points of a parsed test file; rel_path anchors its relative imports."""
    extractor = _Extractor(nodes, rel_path)
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        name = _callee(node.func)
        if not name:
            continue
        last = name.rsplit(".", 1)[-1]
        receiver = name.rsplit(".", 1)[0] if "." in name else ""
        if (
            receiver
            and receiver.split(".")[0] not in _NOT_MONKEYPATCH
            and last in _MONKEYPATCH_CALLS
        ):
            extractor.monkeypatch(node, name, last)
        elif (
            name in _MOCK_PLAIN
            or name.endswith(_MOCK_FORMS)
            or name
            in {
                "patch.object",
                "patch.dict",
                "patch.multiple",
            }
        ):
            extractor.mock(node, name)
    return extractor.points
