"""Check file privacy and explicit component entry owners without executing imports.

This checker has no baseline or exemptions. Its CLI returns failure for every
violation and unresolved recognized import. Hook activation follows migration
of the actual repository; running this module never implies that migration is done.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard, cast

from scripts.codegen.sdk_surface.contracts import MemberProof, Unknown, query
from scripts.structure import patch_points, placement
from scripts.structure.imports import bindings, executed, facts, normalize

__all__ = [
    "Component",
    "Contracts",
    "Violation",
    "audit_module",
    "entry_members",
    "read_components",
]

# Python 3.12 reference/datamodel.html metadata and data-model protocols,
# not arbitrary double-underscore
# spellings or framework metadata such as __dataclass_fields__/__all_for_ava__.
_MODULE_PROTOCOL = frozenset(
    {"__name__", "__doc__", "__file__", "__path__", "__package__"}
    | {"__spec__", "__loader__", "__cached__", "__builtins__"}
    | {"__annotations__", "__all__", "__dict__", "__getattr__", "__dir__"}
)
_OBJECT_PROTOCOL = frozenset(
    {"__new__", "__name__", "__qualname__", "__module__", "__doc__"}
    | {"__annotations__", "__dict__", "__class__", "__slots__", "__weakref__"}
    | {"__match_args__", "__mro__", "__bases__", "__base__", "__subclasses__"}
    | {"__globals__", "__closure__", "__defaults__", "__kwdefaults__", "__code__"}
    | {"__self__", "__func__", "__type_params__", "__init__", "__del__", "__repr__"}
    | {"__str__", "__bytes__", "__format__", "__lt__", "__le__", "__eq__", "__ne__"}
    | {"__gt__", "__ge__", "__hash__", "__bool__", "__getattribute__", "__getattr__"}
    | {"__setattr__", "__delattr__", "__dir__", "__get__", "__set__", "__delete__"}
    | {"__set_name__", "__init_subclass__", "__class_getitem__", "__instancecheck__"}
    | {"__subclasscheck__", "__call__", "__len__", "__length_hint__", "__getitem__"}
    | {"__setitem__", "__delitem__", "__missing__", "__iter__", "__next__"}
    | {"__reversed__", "__contains__", "__add__", "__sub__", "__mul__", "__matmul__"}
    | {"__truediv__", "__floordiv__", "__mod__", "__divmod__", "__pow__", "__lshift__"}
    | {"__rshift__", "__and__", "__xor__", "__or__", "__radd__", "__rsub__", "__rmul__"}
    | {"__rmatmul__", "__rtruediv__", "__rfloordiv__", "__rmod__", "__rdivmod__"}
    | {"__rpow__", "__rlshift__", "__rrshift__", "__rand__", "__rxor__", "__ror__"}
    | {"__iadd__", "__isub__", "__imul__", "__imatmul__", "__itruediv__"}
    | {"__ifloordiv__", "__imod__", "__ipow__", "__ilshift__", "__irshift__"}
    | {"__iand__", "__ixor__", "__ior__", "__neg__", "__pos__", "__abs__", "__invert__"}
    | {"__complex__", "__int__", "__float__", "__index__", "__round__", "__trunc__"}
    | {"__floor__", "__ceil__", "__enter__", "__exit__", "__await__", "__aiter__"}
    | {"__anext__", "__aenter__", "__aexit__", "__buffer__", "__release_buffer__"}
)
_IMPORT_KINDS = frozenset(
    {
        facts.FactKind.IMPORT,
        facts.FactKind.DYNAMIC_IMPORT,
        facts.FactKind.EMBEDDED_IMPORT,
        facts.FactKind.PYTHON_MODULE,
    }
)
_LANGUAGE_PROTOCOL = _MODULE_PROTOCOL | _OBJECT_PROTOCOL


@dataclass(frozen=True)
class Component:
    """A responsibility prefix and its exact entry-definition modules."""

    module: str
    entry_modules: tuple[str, ...]


@dataclass(frozen=True, order=True)
class Violation:
    """A source location and the failed contract; never a permitted baseline site."""

    path: str
    line: int
    target: str
    reason: str


def _dotted(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and all(part.isidentifier() for part in value.split("."))


def _component_record(raw: object) -> Component:
    if not isinstance(raw, dict):
        raise TypeError("Each component must be a table")
    record = cast(dict[str, object], raw)
    if set(record) != {"module", "entry_modules"}:
        raise ValueError("Each component requires exactly module and entry_modules")
    module, raw_entries = record["module"], record["entry_modules"]
    if not _dotted(module) or not isinstance(raw_entries, list):
        raise ValueError("Component and entry modules must be exact dotted identifiers")
    entries: list[str] = []
    for entry in cast(list[object], raw_entries):
        if not _dotted(entry):
            raise ValueError("Entry modules must be exact dotted identifiers")
        entries.append(entry)
    if len(set(entries)) != len(entries):
        raise ValueError("Duplicate entry module")
    if any(entry != module and not entry.startswith(module + ".") for entry in entries):
        raise ValueError("An entry module must belong to its component")
    return Component(module, tuple(entries))


def read_components(config: Mapping[str, object]) -> tuple[Component, ...]:
    """Read exact component declarations, rejecting wildcards and unknown fields."""
    records = config.get("components")
    if not isinstance(records, list) or not records:
        raise ValueError("public_contracts.components must be a nonempty array")
    components = tuple(_component_record(record) for record in cast(list[object], records))
    if len({component.module for component in components}) != len(components):
        raise ValueError("Duplicate component prefix")
    return components


def _module_of(path: str) -> str:
    return path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def _assignment_names(statement: ast.stmt) -> set[str]:
    if isinstance(statement, ast.TypeAlias):
        return {statement.name.id}
    if isinstance(statement, ast.AnnAssign) and statement.value is None:
        return set()  # A type annotation neither binds nor replaces a runtime value.
    if not isinstance(statement, ast.Assign | ast.AnnAssign):
        return set()
    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
    return {target.id for target in targets if isinstance(target, ast.Name)}


def _assignment_definition(statement: ast.stmt) -> set[str]:
    # An alias of an imported member is a re-export, not a definition owner.
    if isinstance(statement, ast.Assign | ast.AnnAssign) and isinstance(
        statement.value, ast.Name | ast.Attribute
    ):
        return set()
    return _assignment_names(statement)


def _definitions(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(statement.name)
        else:
            names.difference_update(_assignment_names(statement))
            names.update(_assignment_definition(statement))
    # A later/conditional import cannot replace the definition with a re-export.
    for node in bindings.local_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            names.difference_update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.difference_update(
                alias.asname or alias.name.split(".", maxsplit=1)[0] for alias in node.names
            )
    return names


def _export_declaration(statement: ast.AST) -> ast.expr | None:
    match statement:
        case ast.Assign(targets=targets, value=value) if any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in targets
        ):
            if len(targets) != 1:
                raise ValueError("Entry __all__ cannot escape through a chained assignment")
            return value
        case ast.AnnAssign(target=ast.Name(id="__all__"), value=value):
            return value
        case _:
            return None


def _export_value(tree: ast.Module) -> ast.List | ast.Tuple:
    declarations: list[ast.expr] = []
    for statement in bindings.local_nodes(tree):
        value = _export_declaration(statement)
        if value is not None:
            if statement not in tree.body:
                raise ValueError("Entry __all__ must be an unconditional module declaration")
            declarations.append(value)
    if len(declarations) != 1 or not isinstance(declarations[0], ast.List | ast.Tuple):
        raise ValueError("Entry owner requires one literal list/tuple __all__")
    return declarations[0]


def _reject_export_mutations(tree: ast.Module) -> None:
    for node in bindings.local_nodes(tree):
        if (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and isinstance(node.value, ast.Name)
            and node.value.id == "__all__"
        ):
            raise ValueError("Entry __all__ cannot escape through an alias")
    for node in ast.walk(tree):
        if _writes_export_list(node):
            raise ValueError("Entry __all__ cannot be mutated")


def _writes_export_list(node: ast.AST) -> bool:
    match node:
        case ast.Subscript(value=ast.Name(id="__all__"), ctx=ast.Store() | ast.Del()):
            return True
        case ast.AugAssign(target=ast.Name(id="__all__")):
            return True
        case ast.Call(func=ast.Attribute(value=ast.Name(id="__all__"))):
            return True
        case _:
            return False


def entry_members(tree: ast.Module) -> frozenset[str]:
    """Read one literal __all__; every member must be defined in this source file."""
    elements = _export_value(tree).elts
    _reject_export_mutations(tree)
    names: list[str] = []
    for element in elements:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            raise TypeError("Entry __all__ members must be literal strings")
        names.append(element.value)
    if len(set(names)) != len(names) or any(
        not name.isidentifier() or name.startswith("_") for name in names
    ):
        raise ValueError("Entry __all__ requires unique non-private identifiers")
    missing = set(names) - _definitions(tree)
    if missing:
        raise ValueError("Entry re-exports or undefined members: " + ", ".join(sorted(missing)))
    return frozenset(names)


class Contracts:
    """Explicit ownership over the checkout's shared module index."""

    def __init__(
        self, components: Sequence[Component], index: placement.ModuleIndex, tops: Sequence[str]
    ) -> None:
        self.components = tuple(sorted(components, key=lambda item: len(item.module), reverse=True))
        self.index, self.tops = index, tuple(tops)
        self._member_cache: dict[str, frozenset[str]] = {}
        self._classes: dict[str, bool] = {}
        self._sdk: dict[str, MemberProof | Unknown] = {}

    def component(self, module: str) -> Component | None:
        return next(
            (
                item
                for item in self.components
                if module == item.module or module.startswith(item.module + ".")
            ),
            None,
        )

    def validate(self) -> tuple[Violation, ...]:
        """Unused declarations also require real entries belonging to their exact owner."""
        violations: list[Violation] = []
        for component in self.components:
            for module in component.entry_modules:
                if self.component(module) != component:
                    violations.append(
                        Violation(
                            "pyproject.toml",
                            1,
                            module,
                            "Parent component cannot grant a child's entry",
                        )
                    )
                    continue
                try:
                    self.members(module)
                except (TypeError, ValueError) as error:
                    violations.append(Violation("pyproject.toml", 1, module, str(error)))
        return tuple(violations)

    def members(self, module: str) -> frozenset[str]:
        if module in self._member_cache:
            return self._member_cache[module]
        path = self.index.file(module)
        if path is None:
            raise ValueError(f"Entry owner has no source file: {module}")
        members = entry_members(
            ast.parse((self.index.repo_root / path).read_text(encoding="utf-8"))
        )
        self._member_cache[module] = members
        return members

    def defines_class(self, target: str) -> bool:
        """Only a direct class definition proves nominal constructor provenance."""
        if target not in self._classes:
            module = self.index.resolve_prefix(target, self.tops)
            path = self.index.file(module) if module else None
            name = target.removeprefix(module + ".") if module else ""
            self._classes[target] = bool(
                path
                and any(
                    isinstance(node, ast.ClassDef) and node.name == name
                    for node in ast.parse(
                        (self.index.repo_root / path).read_text(encoding="utf-8")
                    ).body
                )
            )
        return self._classes[target]

    def check(self, path: str, line: int, target: str) -> Violation | None:
        if not target or target.split(".", maxsplit=1)[0] not in self.tops:
            return None
        module = self.index.resolve_prefix(target, self.tops)
        sdk = target == "ava" or target.startswith("ava.")
        if module is None:
            if sdk:
                private = self._private(target, "ava")
                if private:
                    return Violation(
                        path, line, private, "Private names and modules are file-local"
                    )
                return self._sdk_check(path, line, target)
            return Violation(path, line, target, "Unresolved first-party source owner")
        if self.index.file(module) == path:
            return None
        private = self._private(target, module)
        if private:
            return Violation(path, line, private, "Private names and modules are file-local")
        failure = self._component_check(path, line, target, module)
        return self._sdk_check(path, line, target) if sdk and failure else failure

    def _component_check(self, path: str, line: int, target: str, module: str) -> Violation | None:
        source, owner = self.component(_module_of(path)), self.component(module)
        if source is None or owner is None:
            return Violation(path, line, target, "Unclassified component boundary")
        if source == owner:
            return None
        if module not in owner.entry_modules:
            return Violation(path, line, target, "Cross-component access requires an entry module")
        try:
            members = self.members(module)
        except (TypeError, ValueError) as error:
            return Violation(path, line, target, str(error))
        suffix = target.removeprefix(module).removeprefix(".")
        member = suffix.split(".")[0]
        if member and member not in members and member not in _LANGUAGE_PROTOCOL:
            return Violation(path, line, target, "Member is not in the entry owner's __all__")
        return None

    def _sdk_proof(self, target: str) -> MemberProof | Unknown:
        if target not in self._sdk:
            try:
                self._sdk[target] = query(self.index, target)
            except ValueError as error:
                self._sdk[target] = Unknown(target, "", 0, str(error))
        return self._sdk[target]

    def _sdk_check(self, path: str, line: int, target: str) -> Violation | None:
        proof = self._sdk_proof(target)
        if isinstance(proof, Unknown):
            location = f" at {proof.path}:{proof.line}" if proof.path else ""
            return Violation(
                path, line, target, f"Unproved SDK declaration{location}: {proof.reason}"
            )
        return None

    def _sdk_namespace(self, target: str) -> bool:
        if target != "ava" and not target.startswith("ava."):
            return False
        proof = self._sdk_proof(target)
        return isinstance(proof, MemberProof) and not proof.definition_name

    def _private(self, target: str, module: str) -> str | None:
        parts = target.split(".")
        module_length = len(module.split("."))
        for offset, part in enumerate(parts):
            if part.startswith("_") and (offset < module_length or part not in _LANGUAGE_PROTOCOL):
                return ".".join(parts[: offset + 1])
        return None


class _Access(ast.NodeVisitor):
    def __init__(
        self,
        tree: ast.Module,
        path: str,
        contracts: Contracts,
        evidence: facts.Evidence,
        *,
        scope_path: str | None = None,
    ) -> None:
        self.path, self.contracts = path, contracts
        self.scope_path = path if scope_path is None else scope_path
        self.scope = bindings.Scope(tree, self.scope_path)
        self.dynamic: dict[int, set[str]] = {}
        for fact in evidence.records:
            if fact.kind is facts.FactKind.DYNAMIC_IMPORT and not fact.via:
                self.dynamic.setdefault(fact.line, set()).add(fact.target)
        self.violations: set[Violation] = set()

    def origin(self, node: ast.expr, seen: frozenset[str] = frozenset()) -> str:
        """Resolve import-module values using the shared collector's proved targets."""
        known = self.scope.origin(node)
        if known:
            return known
        if isinstance(node, ast.Attribute):
            base = self.origin(node.value, seen)
            return base + "." + node.attr if base else ""
        if isinstance(node, ast.Name) and node.id not in seen:
            value = self.scope.value(node)
            if value is not node:
                return self.origin(value, seen | {node.id})
        if isinstance(node, ast.Call) and self.scope.origin(node.func) == "importlib.import_module":
            targets = self.dynamic.get(node.lineno, set())
            if len(targets) == 1:
                return next(iter(targets))
            if targets:
                self.violations.add(
                    Violation(
                        self.path,
                        node.lineno,
                        ast.unparse(node),
                        "Imported module value has multiple source owners",
                    )
                )
        if isinstance(node, ast.Call):
            constructor = self.scope.origin(node.func)
            if constructor and self.contracts.defines_class(constructor):
                return constructor
        return ""

    def record(self, line: int, target: str) -> None:
        violation = self.contracts.check(self.path, line, target)
        if violation is not None:
            self.violations.add(violation)

    def nested(self, node: bindings.ScopeNode) -> None:
        outer, inner = bindings.scope_parts(node)
        for expression in outer:
            self.visit(expression)
        parent = self.scope
        self.scope = bindings.Scope(node, self.scope_path, parent.nested_parent())
        for statement in inner:
            self.visit(statement)
        self.scope = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.nested(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.nested(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.nested(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.nested(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self.nested(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self.nested(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self.nested(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self.nested(node)

    def visit_Import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.ImportFrom) and node.level and not self.scope_path:
            self.violations.add(
                Violation(
                    self.path,
                    node.lineno,
                    ast.unparse(node),
                    "Python -c has no relative import package anchor",
                )
            )
            return
        clause = normalize(node, self.scope_path)
        for binding in clause.bindings:
            if binding.name == "*":
                self.violations.add(
                    Violation(
                        self.path,
                        node.lineno,
                        binding.target,
                        "Star imports do not name a component member",
                    )
                )
            else:
                self.record(node.lineno, binding.target)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.visit_Import(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.record(node.lineno, self.origin(node))
        # The outer expression already includes the complete attribute chain.
        if not isinstance(node.value, ast.Attribute):
            self.visit(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        namespace = self.namespace(node.value)
        if namespace:
            self.reflected_member(node.lineno, namespace, node.slice)
        self.generic_visit(node)

    def namespace(self, node: ast.expr, seen: frozenset[str] = frozenset()) -> str:
        """Prove module namespace views, including one unambiguous alias chain."""
        origin = self.origin(node)
        if origin.endswith(".__dict__"):
            return origin.removesuffix(".__dict__")
        if (
            isinstance(node, ast.Call)
            and node.args
            and (
                self.scope.origin(node.func) == "builtins.vars"
                or (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "vars"
                    and not self.scope.bound("vars")
                )
            )
        ):
            return self.origin(node.args[0])
        if isinstance(node, ast.Name) and node.id not in seen:
            value = self.scope.value(node)
            if value is not node:
                return self.namespace(value, seen | {node.id})
        return ""

    def reflected_member(self, line: int, owner: str, name: ast.expr) -> None:
        names = self.scope.strings(name)
        if names is None:
            self.violations.add(
                Violation(self.path, line, owner, "Attribute name is not bounded literal text")
            )
        else:
            for member in names:
                self.record(line, owner + "." + member)

    def call_targets(self, node: ast.expr, seen: tuple[str, ...] = ()) -> tuple[str, ...]:
        if target := self.origin(node):
            return (target,)
        if isinstance(node, ast.Name) and node.id not in seen:
            value = self.scope.value(node)
            if value is not node:
                return self.call_targets(value, (*seen, node.id))
        return self.getter_targets(node)

    def getter_targets(self, node: ast.expr) -> tuple[str, ...]:
        """Resolve the existing bounded getter grammar without executing values."""
        if isinstance(node, ast.Subscript):
            owner, name = self.namespace(node.value), node.slice
        elif isinstance(node, ast.Call) and node.args:
            callee = self.scope.origin(node.func)
            if len(node.args) >= 2 and (
                callee == "builtins.getattr"
                or (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and not self.scope.bound("getattr")
                )
            ):
                owner, name = self.origin(node.args[0]), node.args[1]
            elif isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                owner, name = self.namespace(node.func.value), node.args[0]
            else:
                return ()
        else:
            return ()
        return tuple(owner + "." + name for name in self.scope.strings(name) or ()) if owner else ()

    def visit_Call(self, node: ast.Call) -> None:
        callee = self.scope.origin(node.func)
        for target in self.call_targets(node.func):
            if self.contracts._sdk_namespace(target):
                self.violations.add(
                    Violation(
                        self.path,
                        node.lineno,
                        target,
                        "SDK namespace is not a callable declaration",
                    )
                )
        reflection = {"getattr", "setattr", "delattr", "hasattr"}
        if (
            callee in {"builtins." + name for name in reflection}
            or (
                isinstance(node.func, ast.Name)
                and node.func.id in reflection
                and not self.scope.bound(node.func.id)
            )
        ) and len(node.args) >= 2:
            owner = self.origin(node.args[0])
            if owner:
                self.reflected_member(node.lineno, owner, node.args[1])
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            owner = self.namespace(node.func.value)
            if owner:
                self.reflected_member(node.lineno, owner, node.args[0])
        self.patch_access(node, callee)
        self.generic_visit(node)

    def patch_access(self, node: ast.Call, callee: str) -> None:
        """Reuse patch grammar while resolving each object in its real lexical scope."""
        call = node
        if callee:
            call = ast.Call(
                func=ast.parse(callee, mode="eval").body,
                args=node.args,
                keywords=node.keywords,
                lineno=node.lineno,
            )
        for point in patch_points.extract_points([call], self.path):
            if point.form == "string" and point.dotted is not None:
                self.record(point.line, point.dotted)
            elif point.form == "object" and node.args:
                origin = self.origin(node.args[0])
                if origin:
                    self.record(point.line, origin + ("." + point.attr if point.attr else ""))


def audit_module(tree: ast.Module, path: str, contracts: Contracts) -> tuple[Violation, ...]:
    """Check normalized member access and the shared collector's execution facts."""
    return _audit_source(tree, path, contracts, scope_path=path)


def _audit_source(
    tree: ast.Module, path: str, contracts: Contracts, *, scope_path: str
) -> tuple[Violation, ...]:
    evidence = facts.collect(tree, path, contracts.index, tops=contracts.tops)
    visitor = _Access(tree, path, contracts, evidence, scope_path=scope_path)
    visitor.visit(tree)
    for record in evidence.records:
        if record.kind in _IMPORT_KINDS and record.kind is not facts.FactKind.IMPORT:
            visitor.record(record.line, record.target)
    for unknown in evidence.unknown:
        if unknown.kind in _IMPORT_KINDS:
            visitor.violations.add(
                Violation(
                    path, unknown.line, unknown.expression, "Unproved import: " + unknown.reason
                )
            )
    for source in executed.inputs(tree, path).sources:
        try:
            source_tree = ast.parse(source.text)
        except SyntaxError:
            continue  # The shared collector already retained this invalid-input diagnostic.
        policy_path = path.removesuffix(".py") + "/python_c.py"
        embedded = _audit_source(source_tree, policy_path, contracts, scope_path="")
        visitor.violations.update(
            Violation(path, source.line, item.target, f"Python -c line {item.line}: {item.reason}")
            for item in embedded
        )
    return tuple(sorted(visitor.violations))


def main(argv: Sequence[str] | None = None) -> int:
    """Run an explicit failing audit; no switches suppress individual violations."""
    root = Path(__file__).resolve().parents[2]
    arguments = list(sys.argv[1:] if argv is None else argv)
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    try:
        components = read_components(config["tool"]["ava"]["public_contracts"])
    except (KeyError, TypeError, ValueError) as error:
        sys.stderr.write(f"Invalid public contracts: {error}\n")
        return 1
    index = placement.ModuleIndex(root)
    tops = tuple(
        dict.fromkeys(
            (*placement.CODE_TOPS, *(item.module.split(".", maxsplit=1)[0] for item in components))
        )
    )
    contracts = Contracts(components, index, tops)
    paths = [root / argument for argument in arguments] if arguments else root.rglob("*.py")
    violations = list(contracts.validate())
    for path in paths:
        if not path.is_file():
            sys.stderr.write(f"Not a source file: {path}\n")
            return 1
        rel = path.relative_to(root).as_posix()
        if rel.split("/")[0] not in contracts.tops:
            continue
        violations.extend(audit_module(ast.parse(path.read_text(encoding="utf-8")), rel, contracts))
    for violation in sorted(set(violations)):
        sys.stdout.write(
            f"{violation.path}:{violation.line}: {violation.reason}: {violation.target}\n"
        )
    return int(bool(violations))


if __name__ == "__main__":
    raise SystemExit(main())
