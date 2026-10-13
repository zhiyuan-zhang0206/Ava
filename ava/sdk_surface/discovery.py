"""Children discovery behind the `ava` SDK surface.

The kind predicates, the `_Constant` wrapper, and the module/class child
walkers that power `help()` rendering, SDK-expand discovery, doc linting, and
metering. `agent_visible_names` is the single source of truth the rest of the
framework imports (`agent/graph/prompt/system_prompt.py`, `ava/sdk_surface/metering.py`,
`scripts/content_lint/lint_doc_symbols.py`). Split out of `ava/__init__.py`; the package
entry re-exports the names tests and framework code reach as `ava.<name>`.
"""

import ast as _ast
import inspect
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import Any, ForwardRef

from .const import is_documented_const

# ── Kind predicates ────────────────────────────────────────────────────────


def is_container(obj: Any) -> bool:
    return inspect.ismodule(obj) or inspect.isclass(obj) or isinstance(obj, SimpleNamespace)


def is_element(obj: Any) -> bool:
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


def is_skill_object(obj: Any) -> bool:
    """True if `obj` is a skill proxy or namespace (has `_ava_skill_kind`)."""
    return hasattr(obj, "_ava_skill_kind")


def is_namespace(obj: Any) -> bool:
    """True when `obj` is a module or a SimpleNamespace plugin namespace."""
    return obj is not None and (inspect.ismodule(obj) or isinstance(obj, SimpleNamespace))


# ── Children discovery ─────────────────────────────────────────────────────


def children(
    container: Any, *, hidden_members: frozenset[str] = frozenset()
) -> list[tuple[str, Any]]:
    """Children of a container. `hidden_members` (dotted `ava` paths, e.g.
    ``ava.self.attach``) drops those members from a module/namespace render — a
    render parameter, passed down by `help()`; classes and plain namespaces are
    not filtered."""
    if inspect.ismodule(container):
        return _sorted_children(_module_children(container, hidden_members=hidden_members))
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
    if isinstance(child, _Constant) or is_documented_const(child):
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
    or None when the container declares none (`agent_visible_names` raises then).

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


class UndeclaredSurfaceError(TypeError):
    """A container reached the agent surface without declaring `__all_for_ava__`."""


def agent_visible_names(container: Any) -> list[str]:
    """Agent-visible member names of an `ava` namespace container - the single
    source of truth for `help()` rendering, SDK-expand discovery, doc linting,
    and metering instrumentation.

    Every container declares its surface in `__all_for_ava__` (`_static_agent_surface`): a
    module, a plugin namespace (the installer writes one from the namespace's public vars when
    the plugin declares none) or a dynamic proxy that carries it as a property (`ava.mcps`,
    `ava.skills` and its nodes). A container that declares none is a bug in whoever built it,
    so this raises instead of guessing from `dir()` or the module's routines. The dynamic
    sub-namespaces a module serves via `__getattr__` are never listed - MCP server proxies are
    metered at their call funnel, not enumerated here.

    Raises:
        UndeclaredSurfaceError: `container` declares no `__all_for_ava__` list.
    """
    surface = _static_agent_surface(container)
    if surface is None:
        raise UndeclaredSurfaceError(
            f"{getattr(container, '__name__', type(container).__name__)} declares no "
            "__all_for_ava__ list; every ava namespace must name its agent-visible members"
        )
    return surface


def _module_children(
    mod: Any, *, hidden_members: frozenset[str] = frozenset()
) -> list[tuple[str, Any]]:
    """The children named by `mod`'s `__all_for_ava__` (via `agent_visible_names`, which raises
    when there is none). PEP 224 attribute docstrings are discovered alongside via AST; any
    non-callable/non-class/non-module attr is wrapped as `_Constant` so the renderer can show
    `## NAME: type` instead of falling back to the underlying class's docstring.
    `hidden_members` is the render caller's filter (dotted paths, e.g. ``ava.self.attach``)."""
    annotations = _module_attribute_annotations(mod)
    docs = _module_attribute_docs(mod)
    surface = agent_visible_names(mod)
    if hidden_members:
        # Dotted member paths (``ava.self.attach``) passed by the render
        # caller - the member stays in the surface list (plugin members,
        # SDK-disable, doc linting all keep working) but never renders.
        prefix = f"{mod.__name__}."
        surface = [name for name in surface if f"{prefix}{name}" not in hidden_members]
    return [
        (name, child)
        for name in surface
        if (child := resolve_child(mod, name, annotations, docs)) is not None
    ]


def _is_routine_or_class(attr: Any) -> bool:
    """True when `attr` is a function or a class."""
    return inspect.isfunction(attr) or inspect.isclass(attr)


def resolve_child(
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
    if is_container(attr) or is_element(attr) or is_documented_const(attr):
        return attr
    return _Constant(
        type_str=annotations.get(name, type(attr).__name__),
        doc=docs.get(name, ""),
    )


def _module_ast(mod: ModuleType) -> _ast.Module | None:
    """Parse current source: a plugin reload can preserve the module identity."""
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
    types — take the display name either way. A `NamedTuple` turns its string
    annotations into `ForwardRef`s, so unwrap those to the source text."""
    if isinstance(ann, ForwardRef):
        type_str = ann.__forward_arg__
    elif isinstance(ann, str):
        type_str = ann
    else:
        type_str = getattr(ann, "__name__", str(ann))
    return (name, _Constant(type_str=type_str, doc=""))


def _method_children(cls: type) -> list[tuple[str, Any]]:
    """Methods / nested classes of a class, in sorted name order."""
    members = vars(cls)
    return [
        (name, members[name])
        for name in sorted(members)
        if not name.startswith("_") and _is_routine_or_class(members[name])
    ]
