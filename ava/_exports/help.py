"""The `ava.help()` renderer and exec-sandbox builtin help routing.

`help()` plus the whole render pipeline (target dispatch, stub formatters,
signature/docstring cleaning), plus the predicate and callable shim that route
builtin `help(ava.*)` calls through that renderer. The package entry re-exports
`help` and the `_format_*` helpers tests reach directly, so `import ava;
ava.help(...)` is unchanged. Children discovery lives in
`ava/_exports/discovery.py`; this module imports it (one-way — discovery never
imports help).
"""

import contextvars
import dataclasses as _dataclasses
import enum as _enum
import inspect
import re as _re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .discovery import (
    _children,
    _Constant,
    _is_container,
    _is_documented_const,
    _is_element,
    _is_namespace,
    _is_skill_object,
    agent_visible_names,
)


def help(*targets: Any) -> None:
    """Print docs for SDK targets, e.g. `ava.help(ava.shell)`."""
    import ava as _ava

    if not targets:
        targets = (_ava,)
    for i, target in enumerate(targets):
        if i > 0:
            print()
        _print_target(target)


def is_ava_target(obj: Any) -> bool:
    """True for an Ava module or agent-visible Ava routine/class binding.

    The module-name fast path treats an object's `__module__` value as a trusted
    identity boundary when it is `ava` or begins with `ava.`.
    """
    import ava as _ava

    if obj is _ava:
        return True
    if inspect.ismodule(obj):
        return _is_ava_module_name(getattr(obj, "__name__", None))
    if not (inspect.isroutine(obj) or inspect.isclass(obj)):
        return False
    if _is_ava_module_name(getattr(obj, "__module__", None)):
        return True
    return _search_ava_for_binding(obj) is not None


def _is_ava_module_name(name: Any) -> bool:
    return isinstance(name, str) and (name == "ava" or name.startswith("ava."))


class HelpRouter:
    """Route Ava targets to curated SDK help without changing builtin help."""

    def __init__(self, original_help: Callable[..., Any]) -> None:
        self._original_help = original_help

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        if not args or kwds:
            return self._original_help(*args, **kwds)
        for index, target in enumerate(args):
            if index:
                print()
            if is_ava_target(target):
                help(target)
            else:
                self._original_help(target)
        return None


# ── Target dispatch ─────────────────────────────────────────────────────────
#
# Render format = Python stub. Module target renders as module-level source
# (docstring + children); function target renders as
# `def name(sig): """doc"""`; submodule child renders as `from . import name`
# + orphan docstring. No Markdown headings; the agent gets what it would
# see in source — same shape as training distribution.


def _print_target(target: Any) -> None:
    fqn = _resolve_fqn(target)
    heading = f"{'#' * _heading_depth(fqn)} {fqn}"
    body = _target_body(target, fqn)
    if body:
        print(f"{heading}\n\n{body}")
    else:
        print(heading)


def _target_body(target: Any, fqn: str) -> str:
    """Render the body under the heading for a help() target."""
    if _is_container(target):
        # A top-level module target (the root `ava`, heading depth 1) shows its
        # own docstring; a nested submodule target (ava.X, depth >= 2) drops it
        # — its description is already shown where its parent lists it.
        # Skill proxies/namespaces always show their own doc (which carries
        # the path + full body), regardless of depth.
        include_doc = (_heading_depth(fqn) == 1) or _is_skill_object(target)
        return _format_module_stub(target, include_own_doc=include_doc)
    if _is_element(target):
        # The FQN tail is the `def name` identifier — if target is a
        # plugin-wrapped function, its `__name__` may still be the wrapper's
        # internal symbol (e.g. `_wrapped_read`); directly splitting the FQN
        # to get `read` always matches the heading.
        terminal = fqn.rsplit(".", 1)[-1] if "." in fqn else fqn
        return _format_function_stub(terminal, target)
    if _is_documented_const(target):
        return _format_documented_const_stub(_resolve_const_name(target), target)
    return f"<{type(target).__name__}> {target!r}"


def _heading_depth(fqn: str) -> int:
    """Markdown heading depth from FQN: `ava` → 1, `ava.X` → 2, ..."""
    # Non-`ava.*` FQN (e.g. fake test modules, unresolved repr): default to H1.
    if fqn != "ava" and not fqn.startswith("ava."):
        return 1
    return fqn.count(".") + 1


def _resolve_fqn(target: Any) -> str:
    """Resolve a target's FQN for the heading: `ava` / `ava.X` / `ava.X.y`.

    For container targets (modules / SimpleNamespace plugin namespaces), the
    name is read from `_qualname` (set by `register_namespace`) or `__name__`.
    For element targets (functions), `__module__ + . + __name__` works for
    unwrapped SDK functions; wrapped functions whose `__module__` points back
    to the wrapping plugin are resolved by searching `ava.*` for the binding.
    """
    import ava as _ava

    if target is _ava:
        return "ava"
    if _is_container(target):
        return _container_fqn(target)
    if _is_element(target):
        return _element_fqn(target)
    if _is_documented_const(target):
        return _resolve_const_name(target)
    return repr(target)


def _container_fqn(target: Any) -> str:
    """FQN of a container target: `_qualname` when present (plugin namespaces
    register it), else `__name__`, else `?`."""
    qn = getattr(target, "_qualname", None)
    if isinstance(qn, str) and qn:
        return qn
    name = getattr(target, "__name__", None)
    return name if isinstance(name, str) else "?"


def _element_fqn(target: Any) -> str:
    """FQN of a function target.

    A wrapped function whose `__module__` points back at its wrapping plugin
    is resolved by searching `ava.*` for the binding; the module-qualified
    name is the fallback when the search finds nothing."""
    mod_name = getattr(target, "__module__", "") or ""
    fn_name = getattr(target, "__name__", "?")
    if mod_name == "ava" or mod_name.startswith("ava."):
        return f"{mod_name}.{fn_name}"
    found = _search_ava_for_binding(target)
    if found is not None:
        return found
    return f"{mod_name}.{fn_name}" if mod_name else fn_name


def _search_ava_for_binding(target: Any) -> str | None:
    """Search agent-visible `ava.*` namespaces for a binding that IS `target`.

    Returns the `ava.<sub>.<attr>` form; submodule prefix prefers the
    submodule's `_qualname` (plugin namespaces register that) so the heading
    reflects the agent-facing name rather than the internal module path.
    """
    import ava as _ava

    for mod_name in agent_visible_names(_ava):
        mod = getattr(_ava, mod_name, None)
        if not _is_namespace(mod):
            continue
        binding = _find_agent_visible_binding(mod, mod_name, target)
        if binding is not None:
            return binding
    return None


def _find_agent_visible_binding(mod: Any, mod_name: str, target: Any) -> str | None:
    """Search one namespace's agent-visible bindings for `target` identity.

    The submodule prefix prefers the namespace's `_qualname` (plugin
    namespaces register that) so the heading reflects the agent-facing name
    rather than the internal module path."""
    members = vars(mod) if isinstance(mod, SimpleNamespace) else mod.__dict__
    for attr_name in agent_visible_names(mod):
        attr_value = members.get(attr_name)
        if attr_value is target and (inspect.isroutine(attr_value) or inspect.isclass(attr_value)):
            prefix = getattr(mod, "_qualname", f"ava.{mod_name}")
            return f"{prefix}.{attr_name}"
    return None


def _format_module_stub(mod: Any, *, include_own_doc: bool = True) -> str:
    # Module → Python source stub: optional own docstring + each child as a
    # source-form entry. Children dispatch by kind:
    #   - function   → `def name(sig): "..."`
    #   - submodule  → `from . import name` + orphan docstring
    #   - PEP 224 constant → `name: type` + orphan docstring
    #   - ava.const() value → `name: type = value` + orphan docstring
    #   - class      → `class name:` + indented docstring (methods not recursed)
    #
    # include_own_doc=False drops the module's own top docstring. A nested
    # submodule target (help(ava.X)) sets this: helping it already lists every
    # member with docstring + signature, and the module's own description is
    # already shown where its parent lists it as a child (see
    # _format_submodule_ref). Only a top-level target (the root `ava`) keeps its
    # own docstring — it has no parent listing to carry it.
    pieces: list[str] = []
    doc = inspect.getdoc(mod)
    if doc and include_own_doc:
        if _is_skill_object(mod):
            pieces.append(doc)
        else:
            pieces.append(_format_docstring(doc, indent=""))
    for name, child in _skill_aware_children(mod):
        pieces.append(_format_child(name, child))
    return "\n\n".join(pieces)


def _skill_aware_children(mod: Any) -> list[tuple[str, Any]]:
    """`_children(mod)`, with a skills container's walk kept index-safe.

    Listing the children of `ava.skills` (or of a skill namespace node) resolves
    every node underneath it through the same accessor a deliberate
    `ava.skills.<name>` takes — but only to print each one's heading and
    one-line description, never a body. Resolution records no `skill_invoked`
    attribution at all: the signal fires on first SKILL.md body consumption
    (the lazy `__doc__` loaders in `ava/skills.py`), and this walk reads only
    frontmatter `_description`s, so no suppression scope is needed.

    The module is looked up through `globals()` rather than the bound name:
    `AVA_SDK_DISABLE=skills` deletes that global, and help() must keep rendering
    every other namespace on a cluster that runs without the skills surface."""
    return _children(mod)


def _format_child(name: str, child: Any) -> str:
    formatter = _child_formatter(child)
    if formatter is None:
        return f"# {name}: {type(child).__name__}"
    return formatter(name, child)


def _child_formatter(child: Any) -> Callable[[str, Any], str] | None:
    """Pick the stub formatter for a child entry by its kind, or None for
    an unrenderable value (falls back to a comment line)."""
    if _is_skill_object(child):
        return _format_skill_child
    if inspect.isroutine(child):
        return _format_function_stub
    if _is_namespace(child):
        return _format_submodule_ref
    if _is_documented_const(child):
        return _format_documented_const_stub
    if isinstance(child, _Constant):
        return _format_pep224_const
    if inspect.isclass(child):
        return _format_class_stub
    return None


def _format_function_stub(name: str, fn: Any) -> str:
    sig = _format_signature(fn)
    doc = inspect.getdoc(fn)
    if not doc:
        return f"def {name}{sig}: ..."
    return f"def {name}{sig}:\n{_format_docstring(doc, indent='    ')}"


def _format_submodule_ref(name: str, mod: Any) -> str:
    """Submodule child: `from . import name` + module's full docstring as orphan."""
    lines = [f"from . import {name}"]
    doc = inspect.getdoc(mod)
    if doc:
        lines.append(_format_docstring(doc, indent=""))
    return "\n".join(lines)


def _format_skill_child(name: str, child: Any) -> str:
    """Skill child: a Markdown heading at the child's own FQN depth (the same
    depth rule every other heading follows), description as the body below.

    The heading spells the path in display form (`ava.skills.web-ai:deep-research`);
    heading depth is computed from the loadable FQN so the segment count (not
    the display separators) decides the level.

    The description is ``_description`` (frontmatter) when available, else the
    first line of ``__doc__`` (namespace nodes carry theirs there)."""
    fqn = getattr(child, "__name__", None) or name
    heading = f"{'#' * _heading_depth(fqn)} {_display_skill_fqn(fqn)}"
    desc = getattr(child, "_description", None)
    if not desc:
        doc = inspect.getdoc(child)
        desc = doc.split("\n")[0] if doc else ""
    return f"{heading}\n\n{desc}" if desc else heading


def _display_skill_fqn(fqn: str) -> str:
    """Render a skill FQN in display spelling: `-`/`:` for `_`/`.` past the
    `ava.skills.` prefix (e.g. `ava.skills.web_ai.deep_research` ->
    `ava.skills.web-ai:deep-research`). Non-skill FQNs pass through unchanged."""
    prefix = "ava.skills."
    if not fqn.startswith(prefix):
        return fqn
    return prefix + fqn[len(prefix) :].replace("_", "-").replace(".", ":")


def _format_pep224_const(name: str, const: "_Constant") -> str:
    annotation = f": {const.type_str}" if const.type_str else ""
    lines = [f"{name}{annotation}"]
    if const.doc:
        lines.append(_format_docstring(const.doc, indent=""))
    return "\n".join(lines)


def _format_documented_const_stub(name: str, const: Any) -> str:
    """`ava.const()`-wrapped value: `name: BaseType = value` + docstring.

    A multi-line string constant (e.g. a skill body) renders as a triple-quoted
    block with its docstring as a lead-in, rather than a single `name: T = <...
    thousands of chars on one line>` assignment that reads as a stiff statement.
    """
    base = type(const).__bases__[0]
    value = _home_relative(str(const)) if isinstance(const, Path) else str(const)
    doc = _format_docstring(const.__doc__, indent="") if const.__doc__ else None
    if isinstance(const, str) and "\n" in value:
        return _format_multiline_const_block(name, base, value, doc)
    lines = [f"{name}: {base.__name__} = {value}"]
    if doc:
        lines.append(doc)
    return "\n".join(lines)


def _home_relative(value: str) -> str:
    """Render a path under the home directory `~`-relative: shorter, stable
    across machines, and the agent passes it back unexpanded just fine."""
    home = str(Path.home())
    if value.startswith(home + "/"):
        return "~" + value[len(home) :]
    return value


def _format_multiline_const_block(name: str, base: type, value: str, doc: str | None) -> str:
    """Render a multi-line string constant as a triple-quoted block with its
    docstring as a lead-in, rather than a single `name: T = <thousands of
    chars>` assignment."""
    triple = '"""' if '"""' not in value else "'''"
    block = f"{name}: {base.__name__} = {triple}\n{value}\n{triple}"
    return f"{doc}\n{block}" if doc else block


# Context variable: when True, _format_class_stub renders class name +
# docstring + field annotations + enum values, but skips methods and nested
# classes. Fields stay so the agent still sees attribute names; the full
# contract (methods) is one help(ava.X.ClassName) away. Set by the system
# prompt builder; on-demand help(ava.X) is unaffected.
_COMPACT_CLASSES: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_COMPACT_CLASSES", default=False
)


def _format_class_stub(name: str, cls: type[Any]) -> str:
    # Class child: `class name(Bases):` + indented docstring + indented members
    # (fields as `name: type`, methods as `def name(sig): doc`), the same
    # expansion functions get. Bases are shown (minus object) so an exception's
    # place in its hierarchy is visible without a docstring restating it. The
    # docstring must be the class's OWN — inspect.getdoc() walks the MRO and
    # would render Exception's built-in doc for a deliberately doc-less class.
    head = _class_head(name, cls)
    parts = _class_doc_parts(name, cls)
    if issubclass(cls, _enum.Enum):
        # Enum members are the contract (legal values) — always keep them.
        parts.extend(f"    {m.name} = {m.value!r}" for m in cls)
    else:
        # Compact mode keeps field annotations (_Constant children) so the
        # agent sees attribute names, but drops methods and nested classes.
        parts.extend(_format_class_members(cls))
    if not parts:
        return f"{head} ..."
    return f"{head}\n" + "\n".join(parts)


def _class_head(name: str, cls: type) -> str:
    """Class header line: `class name(Bases):` (object base omitted)."""
    bases = ", ".join(b.__name__ for b in cls.__bases__ if b is not object)
    return f"class {name}({bases}):" if bases else f"class {name}:"


def _class_doc_parts(name: str, cls: type) -> list[str]:
    """The class's OWN docstring as rendered parts — inspect.getdoc() walks
    the MRO and would render Exception's built-in doc for a deliberately
    doc-less class, so read `vars(cls)['__doc__']` instead."""
    own_doc = vars(cls).get("__doc__")
    # A docstring-less dataclass gets an auto-synthesized __doc__ of the form
    # "Name(field: type, ...)" — pure signature noise, not documentation.
    if _is_synthesized_dataclass_doc(name, cls, own_doc):
        return []
    if not own_doc:
        return []
    return [_format_docstring(inspect.cleandoc(own_doc), indent="    ")]


def _is_synthesized_dataclass_doc(name: str, cls: type, own_doc: Any) -> bool:
    """True when `own_doc` is the auto-synthesized dataclass signature doc."""
    return bool(own_doc) and _dataclasses.is_dataclass(cls) and own_doc.startswith(f"{name}(")


def _format_class_members(cls: type) -> list[str]:
    """Indented member entries of a class, honoring compact mode: field
    annotations (_Constant children) stay, methods and nested classes drop."""
    compact = _COMPACT_CLASSES.get()
    parts: list[str] = []
    for child_name, child in _children(cls):
        if compact and not isinstance(child, _Constant):
            continue
        parts.append(_indent(_format_child(child_name, child), "    "))
    return parts


def _indent(text: str, prefix: str) -> str:
    """Prefix every non-empty line of `text` with `prefix`."""
    return "\n".join(f"{prefix}{line}" if line else "" for line in text.split("\n"))


def _resolve_const_name(const: Any) -> str:
    """Best-effort name for a standalone documented-const target."""
    name = getattr(const, "__qualname__", None) or getattr(const, "__name__", None)
    return name if isinstance(name, str) else "value"


def _format_docstring(doc: str, *, indent: str) -> str:
    # Render `doc` as a triple-quoted Python literal at `indent`. Single-line
    # docstring → one-line form. Multi-line → first line on opening triple,
    # dedented body indented under, closing triple on its own line.
    # Defensive: if docstring contains `"""` it would break the quote pair;
    # switch to single-quote triple. Currently no such case in the ava SDK,
    # but plugin authors might hit it.
    triple = '"""' if '"""' not in doc else "'''"
    lines = doc.split("\n")
    if len(lines) == 1:
        return f"{indent}{triple}{lines[0]}{triple}"
    indented_body = "\n".join(f"{indent}{line}" if line else "" for line in lines[1:])
    return f"{indent}{triple}{lines[0]}\n{indented_body}\n{indent}{triple}"


def _format_signature(target: Any) -> str:
    """Stringified signature, cleaned up:
    - `from __future__ import annotations` quote noise on annotations:
      `: 'str | None'` → `: str | None`, `-> 'int'` → `-> int`. The closing
      quote must be followed by a parameter terminator (`,`, `)`, ` =`,
      space, or end-of-string) — that distinguishes an annotation's outer
      quotes from a nested colon-space-quote inside a string default like
      `pattern: str = "a: 'b'"`.
    - `typing.X` prefix stripped for cleaner reading (`typing.Any` → `Any`).
    - enum default reprs rewritten to source form: `<AgentStatus.RUNNING:
      'running'>` → `AgentStatus.RUNNING` (the repr is not valid Python in an
      otherwise source-shaped stub)."""
    try:
        sig = str(inspect.signature(target))
    except (ValueError, TypeError):
        return "(...)"
    sig = _re.sub(r"(: |-> )'([^']*)'(?=[,)= ]|$)", r"\1\2", sig)
    sig = _re.sub(r"<(\w+(?:\.\w+)+): [^<>]*>", r"\1", sig)
    return sig.replace("typing.", "")
