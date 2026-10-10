"""Explicit config-reader facts for the process-domain consumption matrix."""

from __future__ import annotations

import ast
from collections.abc import Mapping

from scripts.structure.imports import bindings

_CONTEXT = "base.agents.context.AvaContext"
_RUNTIME = "langgraph.runtime.Runtime"
_AGENT = "base.host.env.agent_slices.AgentSlices"
_AUTHORITY = "base.config.service_read.ConfigAuthority"
_AUTHORITY_GETTER = "ava.sdk_surface.settings.config_authority"
_BOOT = "base.config.ConfigBoot"
_BOOT_VIEW = "base.config.ConfigBoot.view"


class _ReaderFacts(ast.NodeVisitor):
    def __init__(self, tree: ast.AST, fields: Mapping[str, str], path: str) -> None:
        self.path = path
        self.parents = {
            id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        self.fields = fields
        self.scope = bindings.Scope(tree, path)
        self.annotations: dict[bindings.Scope, dict[str, tuple[ast.expr, bindings.Scope]]] = {}
        self.reads: list[tuple[str, str | None]] = []

    def _annotation(self, node: ast.expr, scope: bindings.Scope) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node = ast.parse(node.value, mode="eval").body
        if isinstance(node, ast.Subscript):
            owner = scope.unmodified_origin(node.value)
            argument = self._annotation(node.slice, scope)
            return f"{owner}[{argument}]" if owner and argument else ""
        return scope.unmodified_origin(node)

    def _written(self, node: ast.expr, scope: bindings.Scope) -> bool:
        """A receiver's attribute mutations invalidate its declared provenance."""
        expression = ast.unparse(node)
        current: bindings.Scope | None = scope
        while current is not None:
            for written in current.attribute_writes:
                target = ast.unparse(written)
                if expression == target or expression.startswith(target + "."):
                    return True
            current = current.parent
        return False

    def _type(
        self, node: ast.expr, scope: bindings.Scope, seen: frozenset[str] = frozenset()
    ) -> str:
        if self._written(node, scope):
            return ""
        if isinstance(node, ast.Name):
            return self._bound_type(node, scope, seen)
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "context"
            and self._type(node.value, scope, seen) == f"{_RUNTIME}[{_CONTEXT}]"
        ):
            return _CONTEXT
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "view"
            and self._type(node.value, scope, seen) == _BOOT
        ):
            return _BOOT_VIEW
        if isinstance(node, ast.Call):
            return self._call_type(node, scope, seen)
        return ""

    def _bound_type(self, node: ast.Name, scope: bindings.Scope, seen: frozenset[str]) -> str:
        if node.id in seen:
            return ""
        if node.id not in scope.stores and scope.parent is not None:
            return self._type(node, scope.parent, seen)
        if scope.stores[node.id] != 1:
            return ""
        value = scope.value(node)
        if value is not node:
            return self._type(value, scope, seen | {node.id})
        annotation = self.annotations.get(scope, {}).get(node.id)
        return self._annotation(*annotation) if annotation else ""

    def _call_type(self, node: ast.Call, scope: bindings.Scope, seen: frozenset[str]) -> str:
        if self._written(node.func, scope):
            return ""
        origin = scope.unmodified_origin(node.func)
        if origin == _BOOT:
            return _BOOT
        if node.args or node.keywords:
            return ""
        if origin == _AUTHORITY_GETTER:
            return _AUTHORITY
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "require_agent"
            and self._type(node.func.value, scope, seen) == _CONTEXT
        ):
            return _AGENT
        return ""

    def _nested(self, node: bindings.ScopeNode) -> None:
        parent = self.scope
        outer, inner = bindings.scope_parts(node)
        for expression in outer:
            self.visit(expression)
        self.scope = bindings.Scope(node, self.path, parent.nested_parent())
        annotations: dict[str, tuple[ast.expr, bindings.Scope]] = {}
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if arg.annotation is not None:
                    annotations[arg.arg] = (arg.annotation, parent)
        self.annotations[self.scope] = annotations
        for statement in inner:
            self.visit(statement)
        self.scope = parent

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._nested(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._nested(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._nested(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._nested(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._nested(node)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._nested(node)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._nested(node)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._nested(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.annotations.setdefault(self.scope, {})[node.target.id] = (
                node.annotation,
                self.scope,
            )
        self.generic_visit(node)

    def _receiver_key(self, node: ast.expr, scope: bindings.Scope) -> str:
        if isinstance(node, ast.Name):
            if node.id not in scope.stores and scope.parent is not None:
                return self._receiver_key(node, scope.parent)
            value = scope.value(node)
            if value is not node and isinstance(value, ast.Name | ast.Attribute):
                return self._receiver_key(value, scope)
            return f"{id(scope)}:{node.id}"
        if isinstance(node, ast.Attribute):
            return f"{self._receiver_key(node.value, scope)}.{node.attr}"
        return str(id(node))

    def _guarded_view(self, node: ast.Attribute, domain: str) -> bool:
        receiver = node.value.value if isinstance(node.value, ast.Attribute) else node.value
        key = self._receiver_key(receiver, self.scope)
        child: ast.AST = node
        parent = self.parents.get(id(child))
        while parent is not None:
            if isinstance(parent, ast.If) and child in parent.body:
                test = parent.test
                if (
                    isinstance(test, ast.Call)
                    and isinstance(test.func, ast.Attribute)
                    and test.func.attr == "has_domain"
                    and self._type(test.func.value, self.scope) == _BOOT_VIEW
                    and self._receiver_key(test.func.value, self.scope) == key
                    and _literal_argument(test, 0, "domain") == domain
                ):
                    return True
            child, parent = parent, self.parents.get(id(parent))
        return False

    def _assert_view_field(self, domain: str, field: str) -> None:
        if field in self.fields:
            valid = self.fields[field] == domain
        else:
            from base.config import Settings

            owner = Settings.model_fields.get(domain)
            valid = owner is not None and hasattr(owner.annotation, field)
        assert valid, f"config read names an undeclared field: {domain}.{field}"

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            isinstance(node.ctx, ast.Load)
            and isinstance(node.value, ast.Attribute)
            and self._type(node.value.value, self.scope) == _BOOT_VIEW
            and not self._written(node, self.scope)
        ):
            domain, field = node.value.attr, node.attr
            self._assert_view_field(domain, field)
            if not self._guarded_view(node, domain):
                self.reads.append((domain, field))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and not self._written(node.func, self.scope):
            receiver = self._type(node.func.value, self.scope)
            if receiver == _AUTHORITY and node.func.attr == "service_field_value":
                field = _literal_argument(node, 0, "name")
                if field is not None:
                    assert field in self.fields, f"config read names an undeclared field: {field}"
                    self.reads.append((self.fields[field], field))
            elif receiver == _AGENT and node.func.attr == "read":
                domain = _literal_argument(node, 0, "domain")
                field = _literal_argument(node, 1, "field")
                if domain is not None:
                    assert domain in self.fields.values(), (
                        f"config read names an undeclared domain: {domain}"
                    )
                    if field is not None:
                        assert self.fields.get(field) == domain, (
                            f"config read names an undeclared field: {domain}.{field}"
                        )
                    self.reads.append((domain, field))
        self.generic_visit(node)


def _literal_argument(node: ast.Call, position: int, name: str) -> str | None:
    argument = (
        node.args[position]
        if len(node.args) > position
        else next((keyword.value for keyword in node.keywords if keyword.arg == name), None)
    )
    return (
        argument.value
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        else None
    )


def explicit_config_reads(source: str, path: str = "") -> list[tuple[str, str | None]]:
    """Literal reads proven by canonical import origins and unmodified receiver types.

    Dynamic agent fields still prove consumption of a literal domain. Unknown
    literal fields/domains fail rather than manufacturing a consumption fact.
    """
    from base.host.env.config_lite_table import FIELD_DOMAINS

    tree = ast.parse(source)
    collector = _ReaderFacts(tree, FIELD_DOMAINS, path)
    collector.visit(tree)
    return collector.reads
