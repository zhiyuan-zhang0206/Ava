"""The data model of the skill tree: one skill's metadata and the namespace nodes that hold it.

Pure values and functions of them - no scan, no files, no ambient state. The scan lives in
`ava/skills/skill_index.py` and the `ava.skills` facade serves the tree to the agent; the
composer commands (`base.packages.composer_commands`) and the agent's prompt read skills as
`Skill` values and render them with `identifier` / `target`.
"""

from __future__ import annotations

from typing import TypedDict

from base.packages.skills.names import display_name, match_key

__all__ = [
    "NS",
    "Leaf",
    "Node",
    "Skill",
    "SkillNameCollision",
    "Tree",
    "claim",
    "descend",
    "flatten",
    "identifier",
    "target",
]


class Skill(TypedDict):
    """Metadata for one skill. `name` is the bare identifier from frontmatter
    (canonically dash-separated). `description` / `path` as usual. `namespace`
    is the tuple of folder segments above the skill (raw folder names; `()` = a
    bare top-level skill). Both fields keep the RAW spelling found on disk —
    `identifier(skill)` renders the canonical dash display id and
    `target(skill)` the `ava.skills` access path."""

    name: str
    description: str
    path: str
    namespace: tuple[str, ...]


class SkillNameCollision(ValueError):  # noqa: N818
    """Two different skill directories under one mount root fold to the same
    namespace path once dash and underscore are unified (`foo-bar/` beside
    `foo_bar/`, or two SKILL.md files claiming the same `name:`).

    The tree can hold only one of them, so the loader refuses rather than
    silently keeping whichever sorted last — a skill vanishing with no signal is
    the failure mode worth crashing over. Rename one; dash is canonical."""


def identifier(skill: Skill) -> str:
    """Composer / display identifier: the `:`-joined dash path, e.g.
    `superpowers:brainstorming`, `coding:tdd`, or a bare `goal` when the skill
    has no namespace.

    Canonical display form, so a skill whose folder or frontmatter still spells
    itself with underscores (a hand-installed legacy package, a plugin whose
    directory must stay a Python package) still presents the ecosystem
    spelling. `:` separates namespace segments in display; `target()` renders
    the loadable `ava.skills` spelling of the same path (`-`/`:` -> `_`/`.`)."""
    return ":".join(display_name(seg) for seg in (*skill["namespace"], skill["name"]))


def target(skill: Skill) -> str:
    """`ava.skills` access path (attr form): `.`-joined, hyphens→underscores,
    e.g. `superpowers.test_driven_development`, or bare `goal`. The Python
    projection of `identifier` — the two are the same path in two spellings."""
    return ".".join(match_key(seg) for seg in (*skill["namespace"], skill["name"]))


# ─── scanning: folder tree = namespace tree ────────────────────────────────
#
# A tree node is either a `Leaf` (a pure skill — folder with SKILL.md, no
# skill-bearing children) or an `NS` (a namespace dict, optionally carrying its
# own `.skill` for a root skill and/or an INDEX.md `.doc`). Both are concrete
# wrapper types so `isinstance` distinguishes them — a TypedDict is a plain dict
# at runtime, so the leaf needs a wrapper, and the namespace a dedicated subclass
# to hang `.skill` / `.doc` off.


class Leaf:
    """A pure skill leaf in the namespace tree — a folder with a SKILL.md and no
    skill-bearing children. Wraps a `Skill` so it's not confused with a
    namespace `dict`."""

    __slots__ = ("skill",)

    def __init__(self, skill: Skill) -> None:
        self.skill = skill


class NS(dict[str, "Node"]):
    """A namespace node — a folder that contains skill-bearing children. A `dict`
    of `child_attr -> (Leaf | NS)` carrying two optional folder-level fields:

      - `skill`: set when the folder ALSO has its own SKILL.md (a *root skill* —
        a folder that is both an invocable skill and a namespace, like a Python
        package whose `__init__` carries a body alongside its submodules).
      - `doc`: an authored namespace description from a sibling INDEX.md (a folder
        that is only a label, like a package `__init__` with just a docstring).
        A root skill's own description takes precedence over INDEX.md.
    """

    skill: Skill | None = None
    doc: str | None = None


type Node = Leaf | NS
type Tree = dict[str, Node]


def descend(tree: Tree, segs: tuple[str, ...]) -> NS:
    """Walk `segs` from `tree`, returning the `NS` at that path (creating nodes
    as needed). A `Leaf` met mid-walk is promoted to an `NS` that keeps the
    leaf's skill — the folder turns out to be a root skill (a skill AND a
    namespace)."""
    node: Tree = tree
    for seg in segs:
        seg_attr = match_key(seg)
        child = node.get(seg_attr)
        if isinstance(child, Leaf):
            promoted = NS()
            promoted.skill = child.skill
            node[seg_attr] = promoted
            child = promoted
        elif not isinstance(child, NS):
            child = NS()
            node[seg_attr] = child
        node = child
    return node  # type: ignore[return-value]  # callers pass a non-empty segs, so node is an NS


def claim(claimed: dict[tuple[str, ...], str], attr_path: tuple[str, ...], src: str) -> None:
    """Record that `src` owns `attr_path` in the tree, refusing a second owner.

    Dash and underscore fold to one attribute path, so `foo-bar/` and `foo_bar/`
    would both land on `tree['foo_bar']` and the later one would silently win.
    Every namespace prefix and every leaf key is claimed by the directory that
    produced it; a different directory claiming the same path raises."""
    prev = claimed.get(attr_path)
    if prev is not None and prev != src:
        raise SkillNameCollision(
            f"skill path {'.'.join(attr_path)!r} is claimed by both {prev} and {src} — "
            "dash and underscore are the same name (base/packages/skills/names.py). "
            "Rename one; dash is canonical."
        )
    claimed[attr_path] = src


def flatten(tree: Tree) -> list[Skill]:
    """All `Skill` entries in the tree, namespace-then-name sorted. A root
    skill's own skill is emitted alongside descending into its children.
    A leaf child whose name matches its parent root skill's name is skipped —
    it was auto-promoted (or is otherwise redundant) and would duplicate the
    root skill already emitted."""
    out: list[Skill] = []

    def walk(node: Tree, parent_skill_name: str | None = None) -> None:
        for key in sorted(node):
            value = node[key]
            if isinstance(value, Leaf):
                # Skip same-named child that duplicates the parent root skill
                if parent_skill_name is not None and key == match_key(parent_skill_name):
                    continue
                out.append(value.skill)
            else:  # NS — a root skill contributes its own skill before its children
                ns_skill_name: str | None = value.skill["name"] if value.skill is not None else None
                if value.skill is not None:
                    out.append(value.skill)
                walk(value, parent_skill_name=ns_skill_name)

    walk(tree)
    return out
