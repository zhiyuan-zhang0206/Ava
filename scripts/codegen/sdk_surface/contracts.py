"""Static SDK declaration provenance, without importing Ava or executing plugins.

Canonical module markers and PluginContributions own the declarations. These
facts describe declared identity, never the installed or enabled runtime surface.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import StrEnum

from scripts.structure.imports import ModuleSourceLookup, bindings

__all__ = ["Availability", "Declaration", "MemberProof", "Unknown", "declared_names", "query"]


class Availability(StrEnum):
    STATIC = "static-declaration"
    PLUGIN = "conditional-plugin-declaration"
    UNKNOWN = "unproved-runtime-surface"


@dataclass(frozen=True)
class Declaration:
    path: str
    line: int
    plugin: str | None = None


@dataclass(frozen=True)
class MemberProof:
    exposed_path: str
    definition_module: str
    definition_name: str
    source_path: str
    source_line: int
    declaration: Declaration
    availability: Availability


@dataclass(frozen=True)
class Unknown:
    exposed_path: str
    path: str
    line: int
    reason: str
    availability: Availability = Availability.UNKNOWN


@dataclass(frozen=True)
class _Definition:
    module: str
    name: str
    path: str
    line: int
    namespace: bool = False
    callable: bool = False


@dataclass(frozen=True)
class _Candidate:
    target: str
    children: tuple[str, ...]
    declaration: Declaration
    namespace: bool


class _UnprovedError(ValueError):
    def __init__(self, path: str, line: int, reason: str) -> None:
        super().__init__(reason)
        self.path = path
        self.line = line


def _marker(tree: ast.Module) -> ast.Assign | ast.AnnAssign | None:
    declarations: list[ast.Assign | ast.AnnAssign] = []
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "__all_for_ava__"
                for target in targets
            ):
                declarations.append(node)
    if not declarations:
        return None
    if len(declarations) != 1 or bindings.Scope(tree, "").stores["__all_for_ava__"] != 1:
        raise ValueError("SDK surface is not one unconditional declaration")
    return declarations[0]


def _marker_reference(
    node: ast.expr, scope: bindings.Scope, seen: frozenset[str] = frozenset()
) -> bool:
    if not isinstance(node, ast.Name) or node.id in seen:
        return False
    if node.id == "__all_for_ava__":
        return True
    value = scope.value(node)
    return _marker_reference(value, scope, seen | {node.id}) if value is not node else False


def _marker_mutated(tree: ast.Module) -> bool:
    scope = bindings.Scope(tree, "")
    for node in bindings.local_nodes(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _marker_reference(node.func.value, scope)
        ):
            return True
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and _marker_reference(node.value, scope)
        ):
            return True
        if (
            isinstance(node, ast.Name)
            and node.id == "__all_for_ava__"
            and isinstance(node.ctx, ast.Del)
        ):
            return True
    return False


def declared_names(tree: ast.Module) -> tuple[str, ...] | None:
    """Read a literal canonical marker; absent markers have no static proof.

    Opaque or locally mutated markers raise ValueError instead of granting the
    runtime discovery fallback. Private names remain hidden, as in discovery.
    """
    marker = _marker(tree)
    if marker is None:
        return None
    if not isinstance(marker.value, ast.List) or _marker_mutated(tree):
        raise ValueError("SDK surface is not one unchanged literal list")
    names: list[str] = []
    for item in marker.value.elts:
        if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
            raise _UnprovedError("", 0, "SDK surface contains an unproved member expression")
        if not item.value.isidentifier():
            raise ValueError("SDK surface contains an invalid member name")
        if not item.value.startswith("_"):
            names.append(item.value)
    return tuple(names)


class _Sources:
    def __init__(self, index: ModuleSourceLookup) -> None:
        self.index = index
        self.trees: dict[str, ast.Module] = {}

    def source(self, module: str) -> tuple[str, ast.Module] | None:
        path = self.index.file(module)
        if path is None:
            return None
        if path not in self.trees:
            try:
                self.trees[path] = ast.parse(
                    (self.index.repo_root / path).read_text(encoding="utf-8")
                )
            except SyntaxError as exc:
                raise _UnprovedError(
                    path, exc.lineno or 1, "Source has invalid Python syntax"
                ) from exc
        return path, self.trees[path]

    def origin(
        self, module: str, name: str, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> _Definition | None:
        if (module, name) in seen or (source := self.source(module)) is None:
            return None
        path, tree = source
        scope = bindings.Scope(tree, path)
        if not _top_bound(tree, path, name) or _deleted(tree, name):
            return _module_property(module, path, tree, name)
        if scope.stores[name] == 1:
            owned = _owned_definition(module, path, tree, name)
            if owned is not None:
                return owned
            value = scope.value(ast.Name(id=name, ctx=ast.Load()))
            if isinstance(value, ast.Name) and value.id != name:
                return self.origin(module, value.id, seen | {(module, name)})
        imported = scope.origin(ast.Name(id=name, ctx=ast.Load()))
        if imported:
            return self.target(imported, seen | {(module, name)})
        return _module_property(module, path, tree, name) if scope.stores[name] <= 1 else None

    def target(
        self, target: str, seen: frozenset[tuple[str, str]] = frozenset()
    ) -> _Definition | None:
        module, _, name = target.rpartition(".")
        parent = self.source(module)
        if parent is not None:
            scope = bindings.Scope(parent[1], parent[0])
            if scope.bound(name) and scope.origin(ast.Name(id=name, ctx=ast.Load())) != target:
                return self.origin(module, name, seen)
        source = self.source(target)
        if source is not None:
            return _Definition(target, "", source[0], 1, namespace=True)
        return self.origin(module, name, seen) if module else None

    def names(self, module: str) -> tuple[tuple[str, ...], Declaration]:
        source = self.source(module)
        if source is None:
            raise _UnprovedError("", 0, "Namespace has no static source owner")
        path, tree = source
        try:
            names = declared_names(tree)
        except ValueError as exc:
            raise _UnprovedError(path, 1, str(exc)) from exc
        marker = _marker(tree)
        if names is None or marker is None:
            raise _UnprovedError(path, 1, "Namespace has no literal canonical SDK marker")
        return names, Declaration(path, marker.lineno)

    def walk(self, module: str, parts: tuple[str, ...]) -> tuple[_Definition, Declaration]:
        names, declaration = self.names(module)
        if not parts:
            return _Definition(module, "", declaration.path, 1, namespace=True), declaration
        name, *children = parts
        if name not in names:
            raise _UnprovedError(
                declaration.path, declaration.line, "Member is absent from the canonical SDK marker"
            )
        origin = self.origin(module, name)
        if origin is None:
            raise _UnprovedError(
                declaration.path,
                declaration.line,
                "Declared member has no unambiguous definition owner",
            )
        if not children:
            return origin, declaration
        if not origin.namespace:
            raise _UnprovedError(
                declaration.path,
                declaration.line,
                "Attribute traversal is not a declared module namespace",
            )
        return self.walk(origin.module, tuple(children))

    def plugins(self, exposed: str) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        for path in sorted((self.index.repo_root / "ava_builtins/plugins").glob("*/plugin.py")):
            rel = path.relative_to(self.index.repo_root).as_posix()
            module = rel.removesuffix(".py").replace("/", ".")
            source = self.source(module)
            if source is not None:
                candidates.extend(_plugin_candidates(source[1], rel, module, exposed))
        return candidates


def _top_bound(tree: ast.Module, path: str, name: str) -> bool:
    direct = (
        ast.Assign,
        ast.AnnAssign,
        ast.Import,
        ast.ImportFrom,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
    )
    return any(
        bindings.Scope(ast.Module(body=[node], type_ignores=[]), path).stores[name]
        for node in tree.body
        if isinstance(node, direct)
    )


def _deleted(tree: ast.Module, name: str) -> bool:
    return any(
        isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Del)
        for node in bindings.local_nodes(tree)
    )


def _owned_definition(module: str, path: str, tree: ast.Module, name: str) -> _Definition | None:
    for node in tree.body:
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.name == name
        ):
            return _Definition(module, name, path, node.lineno, callable=True)
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if (
            any(isinstance(target, ast.Name) and target.id == name for target in targets)
            and node.value is not None
            and not isinstance(node.value, ast.Name | ast.Attribute)
        ):
            return _Definition(module, name, path, node.lineno)
    return None


def _module_class_name(node: ast.stmt, scope: bindings.Scope) -> str | None:
    if (
        not isinstance(node, ast.Assign)
        or len(node.targets) != 1
        or not isinstance(node.value, ast.Name)
    ):
        return None
    target = node.targets[0]
    if not isinstance(target, ast.Attribute) or target.attr != "__class__":
        return None
    slot = target.value
    if not isinstance(slot, ast.Subscript) or scope.origin(slot.value) != "sys.modules":
        return None
    if isinstance(slot.slice, ast.Name) and slot.slice.id == "__name__":
        return node.value.id
    return None


def _module_class(tree: ast.Module, path: str) -> ast.ClassDef | None:
    scope = bindings.Scope(tree, path)
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    selected: list[ast.ClassDef | None] = []
    for node in tree.body:
        name = _module_class_name(node, scope)
        if name is None:
            continue
        cls = classes.get(name)
        if (
            cls is not None
            and scope.stores[name] == 1
            and any(scope.origin(base) == "types.ModuleType" for base in cls.bases)
        ):
            selected.append(cls)
        else:
            selected.append(None)
    return selected[0] if len(selected) == 1 else None


def _property_decorator(decorator: ast.expr, name: str) -> bool:
    return (isinstance(decorator, ast.Name) and decorator.id == "property") or (
        isinstance(decorator, ast.Attribute)
        and decorator.attr in {"setter", "deleter"}
        and isinstance(decorator.value, ast.Name)
        and decorator.value.id == name
    )


def _property_replaced(cls: ast.ClassDef, path: str, name: str) -> bool:
    if name in bindings.Scope(cls, path).values:
        return True
    return any(
        isinstance(node, ast.FunctionDef)
        and node.name == name
        and not any(_property_decorator(decorator, name) for decorator in node.decorator_list)
        for node in cls.body
    )


def _module_property(module: str, path: str, tree: ast.Module, name: str) -> _Definition | None:
    cls = _module_class(tree, path)
    if cls is None or bindings.Scope(tree, path).bound("property"):
        return None
    if bindings.Scope(cls, path).bound("property") or _property_replaced(cls, path, name):
        return None
    getters = [
        member
        for member in cls.body
        if isinstance(member, ast.FunctionDef)
        and member.name == name
        and any(
            isinstance(decorator, ast.Name) and decorator.id == "property"
            for decorator in member.decorator_list
        )
    ]
    if len(getters) != 1:
        return None
    return _Definition(module, f"{cls.name}.{name}", path, getters[0].lineno)


def _arguments(call: ast.Call, fields: tuple[str, ...]) -> dict[str, ast.expr] | None:
    if len(call.args) > len(fields):
        return None
    arguments = dict(zip(fields, call.args, strict=False))
    for keyword in call.keywords:
        if keyword.arg not in fields or keyword.arg in arguments:
            return None
        arguments[keyword.arg] = keyword.value
    return arguments


def _literal_name(node: ast.expr | None) -> str | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.isidentifier()
        and not node.value.startswith("_")
    ):
        return node.value
    return None


def _namespace_candidate(
    scope: bindings.Scope, call: ast.Call, path: str, module: str, exposed: str
) -> _Candidate | None:
    arguments = _arguments(call, ("name", "module", "expand"))
    if arguments is None or (name := _literal_name(arguments.get("name"))) is None:
        return None
    prefix = f"ava.{name}"
    if exposed != prefix and not exposed.startswith(prefix + "."):
        return None
    children = tuple(exposed[len(prefix) :].split(".")[1:])
    target = scope.origin(arguments["module"]) if "module" in arguments else ""
    return _Candidate(target, children, Declaration(path, call.lineno, module), namespace=True)


def _member_candidate(
    scope: bindings.Scope, call: ast.Call, path: str, module: str, exposed: str
) -> _Candidate | None:
    arguments = _arguments(call, ("namespace", "name", "fn"))
    if arguments is None:
        return None
    namespace, name = (_literal_name(arguments.get(key)) for key in ("namespace", "name"))
    if namespace is None or name is None or exposed != f"ava.{namespace}.{name}":
        return None
    expression = arguments.get("fn")
    target = scope.origin(expression) if expression is not None else ""
    if not target and isinstance(expression, ast.Name) and not scope.stores[expression.id]:
        target = f"{module}.{expression.id}"
    return _Candidate(target, (), Declaration(path, call.lineno, module), namespace=False)


def _plugin_call(
    scope: bindings.Scope, call: ast.Call, path: str, module: str, exposed: str, field: str
) -> _Candidate | None:
    origin = scope.origin(call.func)
    if field == "sdk_namespaces" and origin == "base.packages.plugins.extensions.SdkNamespace":
        return _namespace_candidate(scope, call, path, module, exposed)
    if field == "sdk_members" and origin == "base.packages.plugins.extensions.SdkMember":
        return _member_candidate(scope, call, path, module, exposed)
    return None


def _plugin_candidates(tree: ast.Module, path: str, module: str, exposed: str) -> list[_Candidate]:
    outer = bindings.Scope(tree, path)
    contribute = outer.function("contribute")
    if contribute is None:
        return []
    function, _ = contribute
    scope = bindings.Scope(function, path, outer)
    candidates: list[_Candidate] = []
    for statement in function.body:
        if not isinstance(statement, ast.Return) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        if scope.origin(call.func) != "base.packages.plugins.extensions.PluginContributions":
            continue
        candidates.extend(_contribution_candidates(scope, call, path, module, exposed))
    return candidates


def _contribution_candidates(
    scope: bindings.Scope, call: ast.Call, path: str, module: str, exposed: str
) -> list[_Candidate]:
    result: list[_Candidate] = []
    for keyword in call.keywords:
        if keyword.arg not in {"sdk_namespaces", "sdk_members"} or not isinstance(
            keyword.value, ast.Tuple | ast.List
        ):
            continue
        for item in keyword.value.elts:
            if isinstance(item, ast.Call):
                candidate = _plugin_call(scope, item, path, module, exposed, keyword.arg)
                if candidate is not None:
                    result.append(candidate)
    return result


def _resolve_plugin(sources: _Sources, exposed: str, candidate: _Candidate) -> _Definition:
    declaration = candidate.declaration
    if candidate.namespace:
        if sources.origin("ava", exposed.split(".")[1]) is not None:
            raise _UnprovedError(
                declaration.path, declaration.line, "Plugin namespace conflicts with a core binding"
            )
        target = sources.target(candidate.target)
        if target is None or not target.namespace:
            raise _UnprovedError(
                declaration.path,
                declaration.line,
                "Plugin namespace is not a statically bound source module",
            )
        return sources.walk(target.module, candidate.children)[0]
    namespace = exposed.split(".")[1]
    parent, _ = sources.walk("ava", (namespace,))
    if not parent.namespace:
        raise _UnprovedError(
            declaration.path,
            declaration.line,
            "Plugin member host is not a declared module namespace",
        )
    sources.names(parent.module)
    if sources.origin(parent.module, exposed.split(".")[2]) is not None:
        raise _UnprovedError(
            declaration.path,
            declaration.line,
            "Plugin member conflicts with an existing host binding",
        )
    origin = sources.target(candidate.target)
    if origin is None or not origin.callable:
        raise _UnprovedError(
            declaration.path,
            declaration.line,
            "Plugin member has no statically defined callable owner",
        )
    return origin


def query(index: ModuleSourceLookup, exposed_path: str) -> MemberProof | Unknown:
    """Prove an exact declared SDK path, retaining opaque runtime inputs as Unknown.

    Static availability means a source declaration, not process availability.
    Plugin availability is conditional on installation; wrappers, configuration,
    skill trees, MCP tools and runtime-generated namespaces are not evaluated.
    """
    parts = exposed_path.split(".")
    if len(parts) < 2 or parts[0] != "ava" or not all(part.isidentifier() for part in parts):
        raise ValueError("SDK query requires an exact ava member path")
    sources = _Sources(index)
    try:
        candidates = sources.plugins(exposed_path)
        if len(candidates) > 1:
            return Unknown(exposed_path, "", 0, "Multiple plugins declare this SDK path")
        if candidates:
            declaration = candidates[0].declaration
            origin = _resolve_plugin(sources, exposed_path, candidates[0])
            availability = Availability.PLUGIN
        else:
            origin, declaration = sources.walk("ava", tuple(parts[1:]))
            availability = Availability.STATIC
        return MemberProof(
            exposed_path,
            origin.module,
            origin.name,
            origin.path,
            origin.line,
            declaration,
            availability,
        )
    except _UnprovedError as exc:
        return Unknown(exposed_path, exc.path, exc.line, str(exc))
