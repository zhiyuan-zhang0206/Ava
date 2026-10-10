"""One parsed module for the ambient-state rule: its import table, its own
definitions, callee-name resolution, and which repo classes carry no state.

Shared by `scan.py` (detection) and `__init__.py` (the gate); it has
no policy of its own. A callee such as `Policy(...)` is resolved through the
module's imports to its defining file and classified there, so a frozen
dataclass built at import is not reported as an ambient instance wherever it is
imported from.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

MUTATORS = frozenset(
    {
        "append", "add", "update", "pop", "popitem", "clear", "setdefault", "extend", "remove",
        "discard", "insert", "appendleft", "popleft", "sort", "reverse", "extendleft",
        "__setitem__", "__delitem__", "difference_update", "intersection_update",
        "symmetric_difference_update", "put", "put_nowait", "get_nowait",
    }
)  # fmt: skip
CONTAINER_CTORS = frozenset(
    {
        "dict", "list", "set", "defaultdict", "deque", "OrderedDict", "Counter", "ChainMap",
        "WeakValueDictionary", "WeakKeyDictionary", "WeakSet", "bytearray", "array",
    }
)  # fmt: skip
_DECLARATIVE_BASES = (
    "Enum",
    "StrEnum",
    "IntEnum",
    "Flag",
    "IntFlag",
    "NamedTuple",
    "TypedDict",
    "Protocol",
)
_VALUE_BASES = frozenset(
    {"NamedTuple", "Enum", "StrEnum", "IntEnum", "Flag", "IntFlag", "TypedDict"}
)
_MAX_REEXPORT_HOPS = 3


# ── AST helpers ────────────────────────────────────────────────────────────


def dotted(node: ast.AST) -> str | None:
    """`a.b.c` for a Name/Attribute chain; a call inside the chain reads `f().c`."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return ".".join([node.id, *reversed(parts)])
    if isinstance(node, ast.Call):
        inner = dotted(node.func)
        if inner is not None:
            return ".".join([f"{inner}()", *reversed(parts)])
    return None


def is_main_guard(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def module_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements that run when the module imports: `if`/`try`/`with`/loops are
    entered, an `if __name__ == "__main__":` block is not."""
    for st in body:
        if isinstance(st, ast.If):
            if not is_main_guard(st.test):
                yield from module_statements(st.body)
                yield from module_statements(st.orelse)
        elif isinstance(st, ast.Try | ast.TryStar):
            for block in (st.body, *(h.body for h in st.handlers), st.orelse, st.finalbody):
                yield from module_statements(block)
        elif isinstance(st, ast.With | ast.AsyncWith):
            yield from module_statements(st.body)
        elif isinstance(st, ast.For | ast.AsyncFor | ast.While):
            yield from module_statements(st.body)
            yield from module_statements(st.orelse)
        else:
            yield st


def target_names(target: ast.AST) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in target_names(element)]
    if isinstance(target, ast.Starred):
        return target_names(target.value)
    return []


def container_kind(value: ast.AST) -> tuple[bool, bool]:
    """(is a mutable container, is empty). A comprehension is a container, never empty."""
    if isinstance(value, ast.Dict):
        return True, not value.keys
    if isinstance(value, ast.List | ast.Set):
        return True, not value.elts
    if isinstance(value, ast.DictComp | ast.ListComp | ast.SetComp):
        return True, False
    if isinstance(value, ast.Call):
        name = dotted(value.func) or ""
        if name in {"dict.fromkeys", "list.copy"}:
            return True, False
        last = name.rsplit(".", 1)[-1]
        if last in CONTAINER_CTORS:
            return True, _is_empty_ctor(value, last)
    return False, False


def _is_empty_ctor(call: ast.Call, ctor: str) -> bool:
    """`dict()`, `defaultdict(list)` and `deque(maxlen=8)` hold nothing yet."""
    positional_allowed = 1 if ctor == "defaultdict" else 0
    return len(call.args) <= positional_allowed and all(k.arg == "maxlen" for k in call.keywords)


# ── classes ────────────────────────────────────────────────────────────────


def _base_name(base: ast.expr) -> str:
    if isinstance(base, ast.Subscript):  # `Protocol[T]`, `Generic[T]`
        base = base.value
    return (dotted(base) or "").rsplit(".", 1)[-1]


def is_declarative_class(cls: ast.ClassDef) -> bool:
    """Enum / NamedTuple / TypedDict / Protocol / dataclass / pydantic model: its
    class-body attributes declare fields, they are not shared state."""
    if any(_base_name(b).endswith(_DECLARATIVE_BASES) for b in cls.bases):
        return True
    if any(_base_name(b).endswith(("BaseModel", "BaseSettings")) for b in cls.bases):
        return True
    return any("dataclass" in ast.unparse(d) for d in cls.decorator_list)


def _is_frozen_dataclass(cls: ast.ClassDef) -> bool:
    for decorator in cls.decorator_list:
        if isinstance(decorator, ast.Call) and (dotted(decorator.func) or "").endswith("dataclass"):
            return any(
                k.arg == "frozen" and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in decorator.keywords
            )
    return False


def _is_frozen_model(cls: ast.ClassDef) -> bool:
    return any(
        isinstance(st, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "model_config" for t in st.targets)
        and "frozen=True" in ast.unparse(st.value)
        for st in cls.body
    )


def _value_kind(cls: ast.ClassDef) -> bool:
    """Frozen dataclass, frozen pydantic model, NamedTuple/Enum/TypedDict: immutable by form."""
    return (
        _is_frozen_dataclass(cls)
        or any(_base_name(b) in _VALUE_BASES for b in cls.bases)
        or _is_frozen_model(cls)
    )


def _stores_instance_state(node: ast.Assign | ast.AugAssign | ast.AnnAssign) -> bool:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return any(
        isinstance(t, ast.Subscript)
        or (
            isinstance(t, ast.Attribute)
            and isinstance(t.value, ast.Name)
            and t.value.id in {"self", "cls"}
        )
        for t in targets
    )


def _mutates_instance_state(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in MUTATORS
        and isinstance(func.value, ast.Attribute)
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id in {"self", "cls"}
    )


def _assigns_instance_state(node: ast.AST) -> bool:
    if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
        return _stores_instance_state(node)
    return isinstance(node, ast.Call) and _mutates_instance_state(node)


def _declares_mutable_field(st: ast.stmt) -> bool:
    if (
        isinstance(st, ast.AnnAssign)
        and isinstance(st.target, ast.Name)
        and "ClassVar" not in ast.unparse(st.annotation)
    ):
        return True  # a declared instance field without frozen=True is mutable
    return (
        isinstance(st, ast.Assign | ast.AnnAssign)
        and st.value is not None
        and container_kind(st.value)[0]
    )


def _is_stateless(cls: ast.ClassDef) -> bool:
    """No method assigns `self.<attr>`, no class-level mutable container, no declared fields."""
    return not any(_assigns_instance_state(n) for n in ast.walk(cls)) and not any(
        _declares_mutable_field(st) for st in cls.body
    )


def _is_value_class(cls: ast.ClassDef) -> bool:
    return _value_kind(cls) or _is_stateless(cls)


# ── one module ─────────────────────────────────────────────────────────────


def module_name(rel: str) -> str:
    parts = rel.removesuffix(".py").split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _from_base(node: ast.ImportFrom, rel: str) -> str | None:
    if node.level == 0:
        return node.module
    package = rel.removesuffix(".py").split("/")[:-1]
    if node.level - 1 > len(package):
        return None
    anchor = package[: len(package) - (node.level - 1)]
    return ".".join([*anchor, *([node.module] if node.module else [])])


_PARSED: dict[tuple[str, int], Module] = {}


class Module:
    """One parsed module: its import table, the names it defines, and callee resolution."""

    def __init__(self, tree: ast.Module, rel: str, repo_root: Path) -> None:
        self.tree = tree
        self.rel = rel
        self.repo_root = repo_root
        self.qualifier = module_name(rel)
        self.imports: dict[str, tuple[str, str | None]] = {}
        self.defs: set[str] = set()
        self.string_constants: set[str] = set()
        self._classes: dict[str, ast.ClassDef] | None = None
        for st in module_statements(tree.body):
            self._bind(st)

    def _bind(self, st: ast.stmt) -> None:
        if isinstance(st, ast.Import | ast.ImportFrom):
            self.bind_import(st)
        elif isinstance(st, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            self.defs.add(st.name)
        elif isinstance(st, ast.Assign | ast.AnnAssign):
            targets = st.targets if isinstance(st, ast.Assign) else [st.target]
            names = [name for target in targets for name in target_names(target)]
            self.defs.update(names)
            if isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
                self.string_constants.update(names)

    def bind_import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    self.imports[alias.asname] = (alias.name, None)
                else:
                    top = alias.name.split(".")[0]
                    self.imports.setdefault(top, (top, None))
            return
        base = _from_base(node, self.rel)
        if base is not None:
            for alias in node.names:
                if alias.name != "*":
                    self.imports[alias.asname or alias.name] = (base, alias.name)

    def full_name(self, node: ast.AST) -> str:
        """The callee's dotted name with its head resolved through the import table."""
        if isinstance(node, ast.Subscript):
            node = node.value
        name = dotted(node)
        if name is None:
            return "<expr>"
        head, _, rest = name.partition(".")
        head = head.removesuffix("()")
        if head in self.imports:
            origin, attr = self.imports[head]
            return ".".join(filter(None, [origin, attr, rest]))
        if head in self.defs:
            return f"{self.qualifier}.{name}"
        return name

    def is_repo_module(self, dotted_name: str) -> bool:
        path = self.repo_root.joinpath(*dotted_name.split("."))
        return path.with_suffix(".py").is_file() or path.is_dir()

    def module_of(self, node: ast.expr) -> str | None:
        """The first-party module an attribute-assignment base names, if any."""
        name = dotted(node)
        if name is None or "()" in name or name.split(".")[0] not in self.imports:
            return None
        full = self.full_name(node)
        return full if self.is_repo_module(full) else None

    def classes(self) -> dict[str, ast.ClassDef]:
        if self._classes is None:
            self._classes = {
                st.name: st
                for st in module_statements(self.tree.body)
                if isinstance(st, ast.ClassDef)
            }
        return self._classes

    def find_class(self, full: str, hops: int = 0) -> ast.ClassDef | None:
        """The repo class a resolved callee names, following re-exports through `__init__`s."""
        definition = self.class_definition(full, hops)
        return definition[1] if definition is not None else None

    def class_definition(self, full: str, hops: int = 0) -> tuple[Module, ast.ClassDef] | None:
        """The defining module and class, using the same bounded import resolution."""
        owner, _, name = full.rpartition(".")
        if not owner:
            return None
        module = self if owner == self.qualifier else load(self.repo_root, owner)
        if module is None:
            return None
        found = module.classes().get(name)
        if found is None and hops < _MAX_REEXPORT_HOPS and name in module.imports:
            origin, attr = module.imports[name]
            return module.class_definition(".".join(filter(None, [origin, attr])), hops + 1)
        return (module, found) if found is not None else None


def load(repo_root: Path, dotted_name: str) -> Module | None:
    """The first-party module `dotted_name` names, parsed once per file version."""
    base = repo_root.joinpath(*dotted_name.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        try:
            stamp = (str(path), path.stat().st_mtime_ns)
        except OSError:
            continue
        if stamp not in _PARSED:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, UnicodeDecodeError, SyntaxError):
                return None
            _PARSED[stamp] = Module(tree, path.relative_to(repo_root).as_posix(), repo_root)
        return _PARSED[stamp]
    return None


def is_value_class(callee: str, module: Module) -> bool:
    """A repo value constructor, or its body-proven direct classmethod constructor.

    Factory proof uses the existing value-class form, not a return annotation or
    a method name. It does not prove constructor argument purity or deep immutability.
    """
    cls = module.find_class(callee)
    if cls is not None:
        return _is_value_class(cls)
    receiver, _, method_name = callee.rpartition(".")
    definition = module.class_definition(receiver)
    if definition is None or not _is_value_class(definition[1]):
        return False
    owner, cls = definition
    method = _direct_classmethod(owner, cls, method_name)
    return method is not None and _returns_bound_class(method)


def _class_binding_names(st: ast.stmt) -> set[str]:
    if isinstance(st, ast.Assign | ast.Delete):
        return {name for target in st.targets for name in target_names(target)}
    if isinstance(st, ast.AnnAssign | ast.AugAssign):
        return set(target_names(st.target))
    if isinstance(st, ast.Import | ast.ImportFrom):
        return {alias.asname or alias.name.split(".")[0] for alias in st.names}
    if isinstance(st, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return {st.name}
    return set()


def _direct_classmethod(
    owner: Module, cls: ast.ClassDef, method_name: str
) -> ast.FunctionDef | None:
    members = [st for st in cls.body if getattr(st, "name", None) == method_name]
    if len(members) != 1 or not isinstance(members[0], ast.FunctionDef):
        return None
    method = members[0]
    if any(
        _class_binding_names(st) & {method_name, "classmethod"}
        for st in module_statements(cls.body)
        if st is not method
    ):
        return None
    if (
        len(method.decorator_list) != 1
        or owner.full_name(method.decorator_list[0]) != "classmethod"
    ):
        return None
    return method


def _returns_bound_class(method: ast.FunctionDef) -> bool:
    """Accept only a single return of the classmethod's untouched bound class."""
    body = method.body
    if ast.get_docstring(method) is not None:
        body = body[1:]
    args = [*method.args.posonlyargs, *method.args.args]
    return (
        bool(args)
        and len(body) == 1
        and isinstance(body[0], ast.Return)
        and isinstance(body[0].value, ast.Call)
        and isinstance(body[0].value.func, ast.Name)
        and body[0].value.func.id == args[0].arg
    )
