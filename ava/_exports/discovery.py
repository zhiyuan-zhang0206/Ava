"""Children discovery behind the `ava` SDK surface.

The kind predicates, the `_Constant` wrapper, and the module/class child
walkers that power `help()` rendering, SDK-expand discovery, doc linting, and
metering. `agent_visible_names` is the single source of truth the rest of the
framework imports (`agent/graph/_system_prompt.py`, `agent/sdk_metering.py`,
`scripts/lint_doc_symbols.py`). Split out of `ava/__init__.py`; the package
entry re-exports the names tests and framework code reach as `ava.<name>`.
"""

import ast as _ast
import inspect
from dataclasses import dataclass
from functools import cache
from types import ModuleType, SimpleNamespace
from typing import Any

from .const import _DOCUMENTED_TYPE_CACHE

# ── Kind predicates ────────────────────────────────────────────────────────


def _is_container(obj: Any) -> bool:
    return inspect.ismodule(obj) or inspect.isclass(obj) or isinstance(obj, SimpleNamespace)


def _is_element(obj: Any) -> bool:
    return inspect.isroutine(obj)


@dataclass(frozen=True)
class _Constant:
    """Module-level constant exposed via PEP 224 attribute docstring.

    `_module_attribute_docs` AST-parses the module source for
    `AnnAssign(target=Name) [Expr(Constant(str))]` patterns and wraps each hit
    in this. Renderer treats `_Constant` instances as a separate child kind:
    header carries the annotation, body the docstring."""

    type_str: str
    doc: str


def _is_documented_const(obj: Any) -> bool:
    """True if `obj`'s type is one minted by `ava.const()`."""
    return type(obj) in _DOCUMENTED_TYPE_CACHE.values()


def _is_skill_object(obj: Any) -> bool:
    """True if `obj` is a skill proxy or namespace (has `_ava_skill_kind`)."""
    return hasattr(obj, "_ava_skill_kind")


def _is_namespace(obj: Any) -> bool:
    """True when `obj` is a module or a SimpleNamespace plugin namespace."""
    return obj is not None and (inspect.ismodule(obj) or isinstance(obj, SimpleNamespace))


# ── Children discovery ─────────────────────────────────────────────────────


def _children(container: Any) -> list[tuple[str, Any]]:
    if inspect.ismodule(container):
        return _sorted_children(_module_children(container))
    if inspect.isclass(container):
        # Class members keep definition order — field order is part of the
        # contract (dataclass rows render in their declared order).
        return _class_children(container)
    if isinstance(container, SimpleNamespace):
        return _sorted_children(
            [(n, v) for n, v in vars(container).items() if not n.startswith("_")]
        )
    return []


def _child_group_rank(child: Any) -> int:
    """Stub layout group, mirroring Python source convention: imports
    (submodules) first, then constants, then classes, then functions."""
    if inspect.ismodule(child) or isinstance(child, SimpleNamespace):
        return 0
    if isinstance(child, _Constant) or _is_documented_const(child):
        return 1
    if inspect.isclass(child):
        return 2
    return 3


def _sorted_children(children: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Deterministic module-stub layout: grouped by kind (`_child_group_rank`),
    alphabetical within each group — instead of whatever order `__all_for_ava__`
    or `vars()` happens to carry."""
    return sorted(children, key=lambda nc: (_child_group_rank(nc[1]), nc[0]))


def _static_agent_surface(container: Any) -> list[str] | None:
    """The explicitly-declared agent surface (`__all_for_ava__`) as a name list,
    or None when the container declares none — the caller then falls back to its
    own discovery (help walks `dir()`, metering walks own routines).

    Read via `getattr_static` so a dynamically-served member is never
    force-evaluated. The one exception is a `property` surface: `ava.mcps`'s
    per-server proxy computes its tool list through a `__all_for_ava__`
    property, so that (and only that) is resolved through normal attribute
    access. Underscore-prefixed names are always dropped, whatever the source,
    so a plugin appending a private helper to `__all_for_ava__` never leaks into
    the agent's view."""
    surface = inspect.getattr_static(container, "__all_for_ava__", None)
    if surface is None:
        return None
    if isinstance(surface, property):
        surface = getattr(container, "__all_for_ava__", None)
    if not isinstance(surface, list):
        return None
    return [n for n in surface if isinstance(n, str) and not n.startswith("_")]


def agent_visible_names(container: Any) -> list[str]:
    """Agent-visible member names of an `ava` namespace container — the single
    source of truth for `help()` rendering, SDK-expand discovery, doc linting,
    and metering instrumentation.

    Prefers the explicit `__all_for_ava__` surface (`_static_agent_surface`); a
    container that declares none falls back to public vars (a `SimpleNamespace`
    plugin namespace) or own public routines (a module without a surface list,
    e.g. `ava.mcps`). The dynamic sub-namespaces a module serves via
    `__getattr__` are never listed — MCP server proxies are metered at their
    call funnel, not enumerated here."""
    surface = _static_agent_surface(container)
    if surface is not None:
        return surface
    if isinstance(container, SimpleNamespace):
        return [n for n in vars(container) if not n.startswith("_")]
    if isinstance(container, ModuleType):
        return [
            n
            for n in dir(container)
            if not n.startswith("_")
            and inspect.isroutine(attr := inspect.getattr_static(container, n, None))
            and getattr(attr, "__module__", "") == container.__name__
        ]
    return []


def _module_children(mod: Any) -> list[tuple[str, Any]]:
    """`__all_for_ava__` whitelist preferred — required for `register_namespace`
    plugin namespaces (SimpleNamespace doesn't satisfy the `dir()` fallback's
    module-prefix check). PEP 224 attribute docstrings discovered alongside via
    AST; any non-callable/non-class/non-module attr is wrapped as `_Constant` so
    the renderer can show `## NAME: type` instead of falling back to the
    underlying class's docstring."""
    annotations = _module_attribute_annotations(mod)
    docs = _module_attribute_docs(mod)
    surface = _static_agent_surface(mod)
    if surface is not None:
        return [
            (name, child)
            for name in surface
            if (child := _resolve_child(mod, name, annotations, docs)) is not None
        ]
    return _module_children_from_dir(mod, annotations, docs)


def _module_children_from_dir(
    mod: Any,
    annotations: dict[str, str],
    docs: dict[str, str],
) -> list[tuple[str, Any]]:
    """Fallback when `mod` has no `__all_for_ava__`: walk `dir(mod)` + filter to entries
    that originate in this package (avoids leaking re-imports like `os` or
    `psycopg` into the help view). Constants discovered via PEP 224 still get
    surfaced — both via `dir()` (when assigned a value) and afterwards by
    sweeping declared-but-unassigned names from the AST."""
    seen: set[str] = set()
    result: list[tuple[str, Any]] = []
    for name in sorted(dir(mod)):
        if name.startswith("_"):
            continue
        child = _classify_dir_entry(mod, name, annotations, docs)
        if child is not None:
            result.append((name, child))
            seen.add(name)
    # PEP 224 declarations without a value miss `dir()`; surface them now.
    for name, doc in docs.items():
        if not name.startswith("_") and name not in seen:
            result.append((name, _Constant(type_str=annotations.get(name, ""), doc=doc)))
    return result


def _classify_dir_entry(
    mod: Any,
    name: str,
    annotations: dict[str, str],
    docs: dict[str, str],
) -> Any:
    """`dir()` fallback classifier: keep entries that look like they belong to
    `mod`'s package, wrap loose constants as `_Constant`."""
    attr = getattr(mod, name, None)
    if attr is None:
        return None
    if inspect.ismodule(attr):
        return attr if _belongs_to_module(attr, mod, name) else None
    if _is_routine_or_class(attr):
        return attr if _defined_in_package(attr, mod) else None
    if _is_documented_const(attr):
        return attr
    if _has_pep224_doc(name, annotations, docs):
        return _Constant(
            type_str=annotations.get(name, type(attr).__name__),
            doc=docs.get(name, ""),
        )
    return None


def _belongs_to_module(attr: Any, mod: Any, name: str) -> bool:
    """True when a module attribute is (a submodule of) `mod`'s package —
    the attribute's name equals or starts with `mod.__name__ + "."`."""
    prefix = f"{mod.__name__}."
    return attr.__name__ == prefix + name or attr.__name__.startswith(prefix)


def _is_routine_or_class(attr: Any) -> bool:
    """True when `attr` is a function or a class."""
    return inspect.isfunction(attr) or inspect.isclass(attr)


def _defined_in_package(obj: Any, mod: Any) -> bool:
    """True when `obj` is defined in `mod` or one of its package submodules."""
    pkg_prefix = mod.__name__.split(".", 1)[0]
    attr_module = getattr(obj, "__module__", None) or ""
    return attr_module == mod.__name__ or attr_module.startswith(pkg_prefix + ".")


def _has_pep224_doc(name: str, annotations: dict[str, str], docs: dict[str, str]) -> bool:
    """True when `name` carries a PEP 224 docstring or type annotation."""
    return name in docs or name in annotations


def _resolve_child(
    mod: Any,
    name: str,
    annotations: dict[str, str],
    docs: dict[str, str],
) -> Any:
    """Build a child entry for `name` from `mod`'s `__all_for_ava__`.

    Functions / classes / modules and `ava.const()`-wrapped values pass
    through; remaining non-callable attrs wrap in `_Constant` so the
    renderer emits `## NAME: type` with the PEP 224 docstring (if any) as
    body. Type comes from the source-level annotation when available,
    falling back to the runtime value's class name."""
    attr = getattr(mod, name, None)
    if attr is None:
        # Names declared via AnnAssign without a value land here — module dict
        # has no entry. Annotation + PEP 224 doc still describe the contract
        # (e.g. `MACHINE` in `ava/self.py` served via `__getattr__`).
        if name in docs or name in annotations:
            return _Constant(type_str=annotations.get(name, ""), doc=docs.get(name, ""))
        return None
    if _is_container(attr) or _is_element(attr) or _is_documented_const(attr):
        return attr
    return _Constant(
        type_str=annotations.get(name, type(attr).__name__),
        doc=docs.get(name, ""),
    )


@cache
def _module_ast(mod: ModuleType) -> _ast.Module | None:
    """Parse `mod`'s source once and cache the AST for downstream extractors."""
    try:
        src = inspect.getsource(mod)
    except (OSError, TypeError):
        return None
    try:
        return _ast.parse(src)
    except SyntaxError:
        return None


def _module_attribute_annotations(mod: ModuleType) -> dict[str, str]:
    """Extract type annotation strings for `AnnAssign` targets in `mod`."""
    tree = _module_ast(mod)
    if tree is None:
        return {}
    annots: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
            annots[node.target.id] = _ast.unparse(node.annotation)
    return annots


def _module_attribute_docs(mod: ModuleType) -> dict[str, str]:
    """Extract PEP 224 attribute docstrings: `{name: docstring}`.

    Matches both `AnnAssign` (typed declaration) and plain `Assign`, when
    immediately followed by an expression statement that is a string literal.
    The literal is treated as the attribute's docstring."""
    tree = _module_ast(mod)
    if tree is None:
        return {}
    body = tree.body
    return {
        name: inspect.cleandoc(literal)
        for i, node in enumerate(body)
        if (name := _named_assignment_target(node)) is not None
        and not name.startswith("_")
        and i + 1 < len(body)
        and (literal := _string_literal_value(body[i + 1])) is not None
    }


def _named_assignment_target(node: _ast.stmt) -> str | None:
    """The name a module-level statement assigns, or None when it is not a
    simple named assignment (an `AnnAssign` or a single-target `Assign`)."""
    if isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
        return node.target.id
    if (
        isinstance(node, _ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], _ast.Name)
    ):
        return node.targets[0].id
    return None


def _string_literal_value(node: _ast.stmt) -> str | None:
    """The string value of a bare string-literal expression statement, or
    None when `node` is not one."""
    if (
        isinstance(node, _ast.Expr)
        and isinstance(node.value, _ast.Constant)
        and isinstance(node.value.value, str)
    ):
        return node.value.value
    return None


def _class_children(cls: type) -> list[tuple[str, Any]]:
    """Public surface of a class, in source-reading order: declared fields
    first (e.g. dataclass attributes — annotation order is `__init__` order),
    then methods / nested classes. Fields wrap as `_Constant` so the renderer
    emits `name: type`; inherited-from-object members don't count."""
    result: list[tuple[str, Any]] = [
        _annotation_field(name, ann)
        for name, ann in inspect.get_annotations(cls).items()
        if not name.startswith("_")
    ]
    result.extend(_method_children(cls))
    return result


def _annotation_field(name: str, ann: Any) -> tuple[str, _Constant]:
    """A declared field as a `_Constant` child. `inspect.get_annotations`
    returns only the class's own annotations, in definition order. Under
    `from __future__ import annotations` they are strings; otherwise real
    types — take the display name either way."""
    type_str = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))
    return (name, _Constant(type_str=type_str, doc=""))


def _method_children(cls: type) -> list[tuple[str, Any]]:
    """Methods / nested classes of a class, in sorted name order."""
    members = vars(cls)
    return [
        (name, members[name])
        for name in sorted(members)
        if not name.startswith("_") and _is_routine_or_class(members[name])
    ]
