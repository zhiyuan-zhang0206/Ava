"""The module objects `ava.skills.<name>` resolves to: a skill leaf and a namespace folder.

Both subclass `types.ModuleType` so `inspect.ismodule` is true and `ava.help()` walks them.
The one thing they need from outside is how a skill's SKILL.md body is consumed (read it and
record the `skill_invoked` attribution): the facade passes that as `consume`, so a test passes
a function that returns a body and records nothing.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import Any

from base.packages.skills.tree import NS, Leaf, Skill

__all__ = ["Consume", "LazySkillDoc", "Namespace", "SkillProxy", "node_to_obj"]

type Consume = Callable[[Skill], str]


class LazySkillDoc:
    """Mixin for skill proxies: `__doc__` loads the SKILL.md body on first
    consumption (a help() render or a direct read) and records the
    `skill_invoked` attribution there — see the module note on attribution
    timing. `__init__` must set `self._doc` to None when the node carries a
    body (index-only nodes set it eagerly in `__init__` and never hit the
    lazy path) and `self._info` to the `Skill` metadata.

    The hook is `__getattribute__` rather than a `__doc__` property: every
    class body auto-assigns its own docstring to `__doc__`, so a base-class
    property named `__doc__` is always shadowed by the subclass's own
    (None-typed) class attribute. The override returns `Any` — the standard
    typing escape hatch for dynamic proxy objects whose attributes are only
    known at runtime."""

    _info: Skill
    _consume: Consume
    _doc: str | None = None

    def __getattribute__(self, name: str) -> Any:
        if name == "__doc__":
            doc = super().__getattribute__("_doc")
            if doc is None:
                doc = super().__getattribute__("_load_skill_doc")()
            return doc
        return super().__getattribute__(name)

    def _load_skill_doc(self) -> str:
        """Read the SKILL.md body, record the `skill_invoked` attribution, and
        cache the result in `_doc`. Runs at most once per proxy (the cache
        makes later reads free, so one proxy records once)."""
        doc = self._consume(self._info)
        self._doc = doc
        return doc


class SkillProxy(LazySkillDoc, types.ModuleType):
    """Runtime object returned for a skill leaf. Also a marker for the
    `ava.help()` renderer (checked via `_ava_skill_kind`).

    `__doc__` carries the skill's full content: path line (tilde-shortened)
    followed by the raw SKILL.md body — so `ava.help(proxy)` renders the
    whole skill without synthetic Python-assignment wrapping. The body loads
    lazily on first `__doc__` access, which is also where the
    `skill_invoked` attribution is recorded (see `LazySkillDoc`).

    `path` is a plain attribute the agent can read directly (e.g. in
    f-strings inside a SKILL.md body); it is hidden from `dir()` and the
    help view so only the content is rendered. `name` is the frontmatter
    name (may contain hyphens).

    ``__name__`` is ``ava.skills.<path>`` so the heading and parent-
    namespace attribution see the fully-qualified name.
    """

    _ava_skill_kind = "leaf"

    def __init__(self, path: str, info: Skill, consume: Consume) -> None:
        super().__init__(f"ava.skills.{path}")
        self._consume = consume
        self._info = info
        self._description = info["description"]
        self.path = info["path"]
        self.name = info["name"]
        self._doc = None  # lazy — SKILL.md body loads on first consumption

    def __dir__(self) -> list[str]:
        return []  # leaf skill — no children to list in help view

    @property
    def __all_for_ava__(self) -> list[str]:
        """A skill leaf has no members; its body is its `__doc__`."""
        return []


class Namespace(LazySkillDoc, types.ModuleType):
    """Runtime object for a namespace node (`ava.skills.<seg>…`). Marker for
    the `ava.help()` renderer (checked via `_ava_skill_kind`).

    Its attrs descend the tree — returning nested `Namespace`s or
    `SkillProxy` leaves — so `ava.skills.<a>.<b>.<skill>` and `ava.help(…)`
    walk naturally.

    A *root skill* node (its folder also has a SKILL.md): `__doc__` carries
    the path line + full SKILL.md body, and children render underneath. The
    body loads lazily on first `__doc__` access — the same attribution point
    as `SkillProxy` — so merely resolving the namespace records nothing. For
    a pure namespace (INDEX.md or no descriptor), `__doc__` is the namespace
    description, already in memory from the scan."""

    _ava_skill_kind = "namespace"

    def __init__(self, path: str, node: NS, consume: Consume) -> None:
        super().__init__(f"ava.skills.{path}")
        self._consume = consume
        self._path = path
        self._tree = node
        if node.skill is not None:
            self._info = node.skill
            self._doc = None  # lazy — SKILL.md body loads on first consumption
            self._description = node.skill["description"]
            self.name = node.skill["name"]
            self.path = node.skill["path"]
        elif node.doc is not None:
            self._doc = node.doc
        else:
            children = sorted(node)
            self._doc = f"Contains: {', '.join(children)}"

    def __getattr__(self, attr: str) -> SkillProxy | Namespace:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr not in self._tree:
            raise AttributeError(
                f"ava.skills.{self._path} has no {attr!r} (available: {sorted(self._tree)})"
            )
        return node_to_obj(f"{self._path}.{attr}", self._tree[attr], self._consume)

    def __dir__(self) -> list[str]:
        return sorted(self._tree)

    @property
    def __all_for_ava__(self) -> list[str]:
        """The skills and folders directly under this namespace."""
        return sorted(self._tree)


def node_to_obj(path: str, node: Leaf | NS, consume: Consume) -> SkillProxy | Namespace:
    """Wrap a tree node: a `Leaf` → `SkillProxy`; an `NS` → `Namespace`.

    Resolution is exposure, not use: no SKILL.md body is read here and no
    `skill_invoked` attribution is recorded. Index renders and system-prompt
    assembly resolve nodes every turn (assembly walks `_flatten` directly) —
    recording here made exposure look like use: ~70% of "loaded" rows had no
    usage trace in the transcript. Attribution fires on first body
    consumption, in the lazy `__doc__` loaders (`_LazySkillDoc`)."""
    if isinstance(node, Leaf):
        return SkillProxy(path, node.skill, consume)
    return Namespace(path, node, consume)
