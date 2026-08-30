"""Reusable instruction packs you read and follow."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

from ava import _skill_sources
from shared.audit_events import SkillInvokedPayload
from shared.install_registry import enabled_skill_names
from shared.log import logger
from shared.paths import ava_home
from shared.skill_index import SkillFile, SkillIndex
from shared.skill_names import SkillIdentity, display_name, match_key

_recorded_skill_invocations: set[tuple[int, str]] = set()
# Per-agent-run dedup set: (agent_id, skill_identifier) tuples — one row per
# skill per run; "loaded" is the only depth the producer writes.

# Attribution timing: a `skill_invoked` row at depth `"loaded"` — the only
# depth the producer writes — means the agent CONSUMED a skill's SKILL.md body
# (a `help()` render, a direct `__doc__` read, or an `ava.files.read` of the
# skill's SKILL.md — see `_record_skill_invoked_by_path`). Resolving a node
# (`ava.skills.<name>` access) is exposure, not use: the system-prompt index
# and every index render walk the same tree every turn and read only
# frontmatter descriptions, so they must record nothing. `_SkillProxy` and
# `_Namespace` therefore load their SKILL.md body lazily on first `__doc__`
# access and record the attribution there. `"loaded"` is the only depth
# ava_self_evolution scores
# (`ava_builtins/skills/ava_self_evolution/reference/collect.py:_skills_touched`);
# baseline prompt exposure is not recorded at all (the `prompt_injected`
# depth was dropped: ~55K rows of "installed on this machine" noise drowned
# the `loaded` signal).


# Skills load as a **namespace tree mirroring folder structure**: every SKILL.md
# is a leaf node, and the folders above it (under its mount point) are its
# namespace path. Mount points:
#   - ~/.ava/skills/                          → tree root (install-registry gated)
#   - provider roots (register_skill_source)  → tree root (runtime, project-local)
#
# `~/.ava/skills/` is THE load dir: repo skills (`<repo>/ava_builtins/skills/*`) and plugin
# skills (`<repo>/ava_builtins/plugins/<p>/skills/*`, `~/.ava/plugins/<p>/skills/*`) are
# synced into it by the skills converge step (`cli/commands/_converge_skills.py`,
# run by `ava start` / `ava cluster update` / `ava converge`); they are not mounted from
# their source trees. A plugin's skills sync under its name
# (`~/.ava/skills/<p>/…`), so the namespace layer survives as plain folder
# structure. The agent never sees "plugins" — only a skill tree whose shape is
# the folder shape.
#
# So `~/.ava/skills/ava-goal/SKILL.md` → ava.skills.ava_goal (bare);
# `~/.ava/skills/coding/tdd/SKILL.md` → ava.skills.coding.tdd;
# `~/.ava/skills/superpowers/brainstorming/SKILL.md` →
# ava.skills.superpowers.brainstorming.
#
# A folder is, like a Python package, both a thing and a container:
#   - SKILL.md, no skill-bearing children   → a plain leaf skill (a module).
#   - SKILL.md AND skill-bearing children    → a *root skill* (a package whose
#     `__init__` has a body): the folder is invocable AND a namespace. `ava.help`
#     on it renders its own SKILL.md, then lists its children.
#   - INDEX.md, no SKILL.md                   → a namespace labelled by an authored
#     description (a package `__init__` with only a docstring). Optional — a bare
#     folder with neither just synthesizes a "contains: …" line.
# A folder never needs both SKILL.md and INDEX.md (SKILL.md's description is the
# label); if both exist the SKILL.md wins.
#
# Two renderings of a skill's location, and one fold between them
# (`shared/skill_names.py` — dash is canonical outward, underscore is the Python
# projection inward):
#   - composer / display identifier — `.`-joined dash form:
#     superpowers.brainstorming, coding.tdd, or bare `goal` (see `identifier`).
#   - Python access path — `.`-joined attr form (hyphens→underscores):
#     ava.skills.superpowers.test_driven_development (see `target`).
# Every dash/underscore comparison in this module goes through `match_key`, so a
# legacy underscore directory and its dash spelling are one name — which is also
# why two directories that fold together are refused (`SkillNameCollision`).
#
# Loading from `~/.ava/skills/` is gated by the install registry
# (enabled_skill_names): a top-level directory with no enabled registry entry —
# hand-copied, or tracked but disabled — is silently ignored (register with
# `ava skill register <name>`). Keeps stray copies from silently loading.
# Provider roots are scanned last (override). The scan is the shared,
# mtime-cached SkillIndex (doorplate ⑤) — see shared/skill_index.py.
# Frontmatter requires name + description; other files in a skill dir are read
# by the agent via ava.files.read.


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


def _skills_dir() -> Path:
    """The skill load dir: `$AVA_HOME/skills/`. Separate function for test monkeypatch convenience."""
    return ava_home() / "skills"


# Plugin-contributed skill-root providers. The registry storage lives in
# `ava._skill_sources` (framework-internal) so the kernel's plugin reload can
# clear it without importing this disable-able `ava.skills` module; the
# functions here are the agent/plugin-facing client over it.


def register_skill_source(provider: Callable[[], list[Path]]) -> None:
    """Plugin extension point: contribute skill roots resolved at scan time.

    `provider()` returns a list of directories; each is mounted at the tree root
    (bare-named) exactly like `<repo>/ava_builtins/skills/`. It is called on every skill scan,
    so a provider may return cwd-dependent roots that change as the agent moves
    between projects. Provider roots are scanned last, so a project-local skill
    overrides a same-named built-in one.
    """
    _skill_sources.register(provider)


def clear_skill_sources() -> None:
    """Drop all registered skill-source providers, so the next plugin load
    re-registers from empty state."""
    _skill_sources.clear()


def _provider_roots() -> list[Path]:
    """Flatten all registered providers' roots into one list (scan order)."""
    return _skill_sources.roots()


# ─── scanning: folder tree = namespace tree ────────────────────────────────
#
# A tree node is either a `_Leaf` (a pure skill — folder with SKILL.md, no
# skill-bearing children) or an `_NS` (a namespace dict, optionally carrying its
# own `.skill` for a root skill and/or an INDEX.md `.doc`). Both are concrete
# wrapper types so `isinstance` distinguishes them — a TypedDict is a plain dict
# at runtime, so the leaf needs a wrapper, and the namespace a dedicated subclass
# to hang `.skill` / `.doc` off.


class _Leaf:
    """A pure skill leaf in the namespace tree — a folder with a SKILL.md and no
    skill-bearing children. Wraps a `Skill` so it's not confused with a
    namespace `dict`."""

    __slots__ = ("skill",)

    def __init__(self, skill: Skill) -> None:
        self.skill = skill


class _NS(dict):
    """A namespace node — a folder that contains skill-bearing children. A `dict`
    of `child_attr -> (_Leaf | _NS)` carrying two optional folder-level fields:

      - `skill`: set when the folder ALSO has its own SKILL.md (a *root skill* —
        a folder that is both an invocable skill and a namespace, like a Python
        package whose `__init__` carries a body alongside its submodules).
      - `doc`: an authored namespace description from a sibling INDEX.md (a folder
        that is only a label, like a package `__init__` with just a docstring).
        A root skill's own description takes precedence over INDEX.md.
    """

    skill: Skill | None = None
    doc: str | None = None


def _descend(tree: dict, segs: tuple[str, ...]) -> _NS:
    """Walk `segs` from `tree`, returning the `_NS` at that path (creating nodes
    as needed). A `_Leaf` met mid-walk is promoted to an `_NS` that keeps the
    leaf's skill — the folder turns out to be a root skill (a skill AND a
    namespace)."""
    node: dict = tree
    for seg in segs:
        seg_attr = match_key(seg)
        child = node.get(seg_attr)
        if isinstance(child, _Leaf):
            promoted = _NS()
            promoted.skill = child.skill
            node[seg_attr] = promoted
            child = promoted
        elif not isinstance(child, _NS):
            child = _NS()
            node[seg_attr] = child
        node = child
    return node  # type: ignore[return-value]  # callers pass a non-empty segs, so node is an _NS


def _claim(claimed: dict[tuple[str, ...], str], attr_path: tuple[str, ...], src: str) -> None:
    """Record that `src` owns `attr_path` in the tree, refusing a second owner.

    Dash and underscore fold to one attribute path, so `foo-bar/` and `foo_bar/`
    would both land on `tree['foo_bar']` and the later one would silently win.
    Every namespace prefix and every leaf key is claimed by the directory that
    produced it; a different directory claiming the same path raises."""
    prev = claimed.get(attr_path)
    if prev is not None and prev != src:
        raise SkillNameCollision(
            f"skill path {'.'.join(attr_path)!r} is claimed by both {prev} and {src} — "
            "dash and underscore are the same name (shared/skill_names.py). "
            "Rename one; dash is canonical."
        )
    claimed[attr_path] = src


class SkillIndexBuilder:
    """Builds the skill namespace tree from mount roots.

    Owns the state the mount threads through — the tree, the per-root
    claimed-paths registry, and the cross-root seen-hashes dedup set — as
    instance state. The folder scan + frontmatter parse itself lives in
    `shared.skill_index` (doorplate ⑤): one builder behind every skill read
    path, so the loader and the lint cannot drift (they had: the INDEX.md gate
    compared raw names where the SKILL.md gate folded dash/underscore, and the
    lint's separate traversal once skipped a three-deep tree entirely).
    """

    def __init__(self, base_ns: tuple[str, ...] = ()) -> None:
        self.tree: dict = {}
        self._base_ns = base_ns
        # attr path -> the source dir that owns it; `mount()` resets it, so a
        # collision is only refused within one root (a later root overriding an
        # earlier one is the documented provider-root behaviour).
        self._claimed: dict[tuple[str, ...], str] = {}
        # SHA256 of every SKILL.md content loaded across all mount points —
        # same content at different paths (e.g. .claude/skills/, .agents/skills/,
        # .ava/skills/ — usually links) must not produce duplicate entries.
        self._seen_hashes: set[str] = set()

    def mount(self, root: Path, gate: set[str] | None) -> None:
        """Scan every `<root>/**/SKILL.md` (+ folder-level `INDEX.md`) into the
        tree, namespaced by folder path under `root` (prefixed by `base_ns`).

        A folder with both a SKILL.md and skill-bearing children becomes a
        *root skill* (its `_NS` carries `.skill`); an `INDEX.md` sets that
        folder's authored namespace `.doc`.

        `gate` not None (the overlay reservation) surfaces only entries whose
        top-level folder under `root` is in the set — compared through
        `match_key` (dash/underscore fold). A malformed SKILL.md is skipped
        with a loud warning rather than crashing the scan; a *name collision*
        is not skipped but raised, because the alternative is a skill
        silently disappearing.
        """
        if not root.is_dir():
            return
        # Claimed paths are per-root: a later root overriding an earlier one
        # is the documented provider-root behaviour, so a collision is only
        # refused between two directories of the SAME mount root.
        self._claimed = {}
        gate_keys = None if gate is None else {match_key(g) for g in gate}
        # The scan is `shared.skill_index` (doorplate ⑤), mtime-cached: this
        # mount runs on every system-prompt rebuild, so the cache turns the
        # repeated scans into a stat walk. Entries arrive parent-before-child
        # (sorted folders) — the arrival order the tree converges under — and
        # include the mount point itself, so a SKILL.md at the root (empty
        # rel) still loads as a bare root skill.
        index = SkillIndex.cached([root])
        for entry in index.entries:
            self._mount_folder_skill(root, entry, gate_keys)
            self._mount_folder_index(entry, gate_keys)

    def _mount_folder_skill(self, root: Path, entry: SkillFile, gate_keys: set[str] | None) -> None:
        """Mount one folder's SKILL.md (if any) as a leaf or root skill.

        `entry` is the folder's materialized scan record (doorplate ⑤) — the
        read + frontmatter parse happened in the index. `gate_keys` the folded
        gate set, or None for no gate. None-returning stages do the skipping:
        the gate, content-hash dedup, and the parse error (malformed /
        unreadable -> warning) each bail out, keeping the pipeline flat.
        """
        skill_md = entry.skill_md
        if skill_md is None:
            return
        rel = entry.rel
        # Gate check: a root SKILL.md at the mount point (empty rel) passes
        # unconditionally — root skills are always available.
        if gate_keys is not None and rel and match_key(rel[0]) not in gate_keys:
            return
        # Dedup by content hash — registered even for a malformed file, so the
        # same content at another path is skipped silently, not warned twice
        # (the pre-index read-then-parse flow behaved the same way).
        if entry.content_hash is not None:
            if entry.content_hash in self._seen_hashes:
                return
            self._seen_hashes.add(entry.content_hash)
        if entry.error is not None:
            logger.warning("skipping malformed skill {}: {}", skill_md, entry.error)
            return
        # Supply-chain gate (audit round-2 up-security-trust P0-1): the same
        # critical rule table the CLI install path refuses on, applied at load
        # time — a project skill (`.claude/skills` etc., mounted with no user
        # action) must not reach the namespace or the system-prompt index.
        if entry.security_rules:
            logger.warning(
                "refusing to mount skill {}: supply-chain scan flagged {}",
                skill_md,
                ", ".join(entry.security_rules),
            )
            return
        name = entry.name
        description = entry.description
        if name is None or description is None:
            # Index contract: error is None only when name+description parsed.
            raise RuntimeError(f"skill index contract violated for {skill_md}")
        skill: Skill = {
            "name": name,
            "description": description,
            "path": str(entry.folder),
            "namespace": (),
        }
        # Identity check (design: install-point identity): the install-point directory is the
        # source of identity, the frontmatter `name:` is the display claim.
        # They must fold to one key — a mismatch means the skill would
        # silently present under a name that is not its own, so it fails
        # fast here (same family as SkillNameCollision) instead of loading
        # under two names. A mount-point root skill (empty rel) has no
        # directory name to check against.
        if rel:
            SkillIdentity.from_dir(rel[-1]).verify(SkillIdentity.from_frontmatter(name))
        ns = self._base_ns + rel[:-1] if rel else self._base_ns
        skill["namespace"] = ns
        parent = _descend(self.tree, ns)
        key = match_key(skill["name"])
        self._claim_paths(root, rel, key, str(skill_md.parent))
        existing = parent.get(key)
        if isinstance(existing, _NS):
            existing.skill = skill  # children mounted first — this folder is a root skill
        else:
            parent[key] = _Leaf(skill)
            self._maybe_promote(parent, key, skill, ns)

    def _mount_folder_index(self, entry: SkillFile, gate_keys: set[str] | None) -> None:
        """Mount one folder's INDEX.md (if any) as its namespace `.doc`.

        A root INDEX.md (empty rel) is unconditionally ignored — the mount
        point's own description is not a namespace label. The gate folds
        dash/underscore like the SKILL.md gate (this is the fix for the old
        INDEX.md loop comparing raw names).
        """
        index_md = entry.index_md
        if index_md is None:
            return
        rel = entry.rel
        if not rel or (gate_keys is not None and match_key(rel[0]) not in gate_keys):
            return
        if entry.index_text is None:
            return  # unreadable INDEX.md: no doc (the pre-index flow crashed here)
        _descend(self.tree, self._base_ns + rel).doc = entry.index_text

    def _claim_paths(self, root: Path, rel: tuple[str, ...], key: str, src: str) -> None:
        """Claim every namespace prefix plus the leaf key for `src`.

        Dash and underscore fold to one attribute path, so `foo-bar/` and
        `foo_bar/` would both land on the same node and the later one would
        silently win. Every namespace prefix and every leaf key is claimed by
        the directory that produced it; a different directory claiming the
        same path raises `SkillNameCollision`.
        """
        attr_path = tuple(match_key(seg) for seg in self._base_ns)
        for depth in range(len(rel) - 1):
            attr_path += (match_key(rel[depth]),)
            _claim(self._claimed, attr_path, str(root.joinpath(*rel[: depth + 1])))
        _claim(self._claimed, (*attr_path, key), src)

    def _maybe_promote(self, parent: _NS, key: str, skill: Skill, ns: tuple[str, ...]) -> None:
        """Auto-promote a leaf skill to a root skill of its parent namespace.

        When a leaf skill has the same name as its parent namespace segment
        and the parent has no own root skill, the leaf is promoted: this turns
        `ava_fleet.ava_fleet` (redundant) into just `ava_fleet` — the folder
        acts as both namespace and root skill without requiring a separate
        SKILL.md at the parent level.
        """
        if ns and parent.skill is None and key == match_key(ns[-1]):
            parent.skill = {
                "name": skill["name"],
                "description": skill["description"],
                "path": skill["path"],
                "namespace": ns[:-1],
            }


def _scan_tree() -> dict:
    """Build the full namespace tree: `~/.ava/skills/` (install-registry gated)
    plus provider roots (scanned last, so a project-local skill overrides a
    same-named converged one).

    A shared `seen_hashes` set tracks SHA256 hashes of every SKILL.md content
    loaded across all mount points, preventing duplicate entries when the same
    skill file content appears at multiple paths (e.g. `.claude/skills/`,
    `.agents/skills/` and `.ava/skills/` in the same repo, where two of the
    three are usually links back to the third).
    """
    builder = SkillIndexBuilder()
    builder.mount(_skills_dir(), enabled_skill_names())
    for root in _provider_roots():
        builder.mount(root, None)
    return builder.tree


def _flatten(tree: dict) -> list[Skill]:
    """All `Skill` entries in the tree, namespace-then-name sorted. A root
    skill's own skill is emitted alongside descending into its children.
    A leaf child whose name matches its parent root skill's name is skipped —
    it was auto-promoted (or is otherwise redundant) and would duplicate the
    root skill already emitted."""
    out: list[Skill] = []

    def walk(node: dict, parent_skill_name: str | None = None) -> None:
        for key in sorted(node):
            value = node[key]
            if isinstance(value, _Leaf):
                # Skip same-named child that duplicates the parent root skill
                if parent_skill_name is not None and key == match_key(parent_skill_name):
                    continue
                out.append(value.skill)
            else:  # _NS — a root skill contributes its own skill before its children
                ns_skill_name: str | None = value.skill["name"] if value.skill is not None else None
                if value.skill is not None:
                    out.append(value.skill)
                walk(value, parent_skill_name=ns_skill_name)

    walk(tree)
    return out


def skills_in(roots: list[Path]) -> list[Skill]:
    """Scan the given roots (mounted bare at the tree root, no overlay gating)
    and return their skills, sorted. Used for project-local provider roots.

    Same parse + fail-fast semantics as the full scan. A shared `seen_hashes`
    set prevents duplicate entries across the given roots.
    """
    builder = SkillIndexBuilder()
    for root in roots:
        builder.mount(root, None)
    return _flatten(builder.tree)


def _names() -> list[Skill]:
    return _flatten(_scan_tree())


# ─── Namespace proxy ──────────────────────────────────────────────────────
#
# `ava.skills.<name>` goes through module-level `__getattr__`, returning a
# `_SkillProxy` (leaf) or a `_Namespace` (folder node). Both subclass
# types.ModuleType so inspect.ismodule = True and ava.help() walks them.


class _LazySkillDoc:
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
        makes later reads free; the per-(agent, skill) dedup in
        `_record_skill_invoked` keeps repeated proxies to one row)."""
        self._doc = _consume_skill_body(self._info)
        return self._doc


class _SkillProxy(_LazySkillDoc, types.ModuleType):
    """Runtime object returned for a skill leaf. Also a marker for the
    `ava.help()` renderer (checked via `_ava_skill_kind`).

    `__doc__` carries the skill's full content: path line (tilde-shortened)
    followed by the raw SKILL.md body — so `ava.help(proxy)` renders the
    whole skill without synthetic Python-assignment wrapping. The body loads
    lazily on first `__doc__` access, which is also where the
    `skill_invoked` attribution is recorded (see `_LazySkillDoc`).

    `path` is a plain attribute the agent can read directly (e.g. in
    f-strings inside a SKILL.md body); it is hidden from `dir()` and the
    help view so only the content is rendered. `name` is the frontmatter
    name (may contain hyphens).

    ``__name__`` is ``ava.skills.<path>`` so the heading and parent-
    namespace attribution see the fully-qualified name.
    """

    _ava_skill_kind = "leaf"

    def __init__(self, path: str, info: Skill) -> None:
        super().__init__(f"ava.skills.{path}")
        self._info = info
        self._description = info["description"]
        self.path = info["path"]
        self.name = info["name"]
        self._doc = None  # lazy — SKILL.md body loads on first consumption

    def __dir__(self) -> list[str]:
        return []  # leaf skill — no children to list in help view


class _Namespace(_LazySkillDoc, types.ModuleType):
    """Runtime object for a namespace node (`ava.skills.<seg>…`). Marker for
    the `ava.help()` renderer (checked via `_ava_skill_kind`).

    Its attrs descend the tree — returning nested `_Namespace`s or
    `_SkillProxy` leaves — so `ava.skills.<a>.<b>.<skill>` and `ava.help(…)`
    walk naturally.

    A *root skill* node (its folder also has a SKILL.md): `__doc__` carries
    the path line + full SKILL.md body, and children render underneath. The
    body loads lazily on first `__doc__` access — the same attribution point
    as `_SkillProxy` — so merely resolving the namespace records nothing. For
    a pure namespace (INDEX.md or no descriptor), `__doc__` is the namespace
    description, already in memory from the scan."""

    _ava_skill_kind = "namespace"

    def __init__(self, path: str, node: _NS) -> None:
        super().__init__(f"ava.skills.{path}")
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

    def __getattr__(self, attr: str) -> _SkillProxy | _Namespace:
        if attr.startswith("_"):
            raise AttributeError(attr)
        if attr not in self._tree:
            raise AttributeError(
                f"ava.skills.{self._path} has no {attr!r} (available: {sorted(self._tree)})"
            )
        return _node_to_obj(f"{self._path}.{attr}", self._tree[attr])

    def __dir__(self) -> list[str]:
        return sorted(self._tree)


def _consume_skill_body(skill: Skill) -> str:
    """Read one skill's SKILL.md body, record the `skill_invoked` attribution,
    and return the consumed shape — a path line (tilde-shortened) then the raw
    body, the shape a proxy's `__doc__` carries. The one body-consumption
    point shared by the lazy proxies and `read()` (dedup is in
    `_record_skill_invoked`), so every access path records identically."""
    from pathlib import Path as _Path

    info = skill
    content = (_Path(info["path"]) / "SKILL.md").read_text(encoding="utf-8")
    _record_skill_invoked(info)
    home = str(_Path.home())
    short = f"~{info['path'][len(home) :]}" if info["path"].startswith(home + "/") else info["path"]
    return f"{short}\n\n{content}"


def _record_skill_invoked_by_path(path: str | Path) -> bool:
    """Record the `skill_invoked` attribution for a direct SKILL.md file read.

    The `.path` + `ava.files.read(SKILL.md)` pattern consumes a skill body like
    a `help()` render, but never touches the lazy proxy hook that carries the
    attribution — so it used to record nothing and under-counted `loaded`
    (facility-wide scan: 68 agents / 3 days used the pattern, 13 with zero
    rows). `ava.files.read` routes here. Only a loaded skill's own SKILL.md
    qualifies (a SKILL.md outside the mount roots is not a skill the agent
    loaded); best-effort end to end, so this never raises. Returns whether a
    matching skill was found and attributed."""
    p = Path(path).expanduser().absolute()
    if p.name != "SKILL.md":
        return False
    for skill in _names():
        if Path(skill["path"]).expanduser().absolute() == p.parent:
            _record_skill_invoked(skill)
            return True
    return False


def read(name: str) -> str:
    """Read a skill's SKILL.md body by name and record the `skill_invoked`
    attribution — the explicit API for consuming a skill body.

    `name` accepts the display identifier (`"web-ai:deep-research"`) or any
    spelling that folds to it (`"web_ai.deep_research"`, bare frontmatter name
    for a flat skill). Returns the same shape a proxy's `__doc__` carries.
    Unknown names raise ValueError."""
    from ava._sdk_validation import coerce_str

    key = match_key(coerce_str(name, "name"))
    for skill in _names():
        if match_key(identifier(skill)) == key or match_key(skill["name"]) == key:
            return _consume_skill_body(skill)
    raise ValueError(f"no skill named {name!r} — `ava.help(ava.skills)` lists the loaded catalog")


def _record_skill_invoked(skill: Skill) -> None:
    """Best-effort record that this agent opened this skill (a hard signal for
    skill attribution) — the `"loaded"` depth, the only depth the producer
    writes. Deduplicates: repeated `ava.skills.X` access within the same agent
    run only emits one event per skill. Skipped silently outside an agent
    process; a write failure is logged and swallowed.
    """
    from ava._boot import require_agent_id

    try:
        agent = require_agent_id()
    except RuntimeError:
        return

    key = (agent, identifier(skill))
    if key in _recorded_skill_invocations:
        return
    # Dedup only on a write that landed: marking first turns one swallowed DB
    # blip into a permanent "already recorded" for the rest of the agent run.
    if _insert_skill_events(agent, [skill]):
        _recorded_skill_invocations.add(key)


def _insert_skill_events(agent: int, skills: list[Skill]) -> bool:
    """Enqueue one `skill_invoked` audit event per skill; return whether the
    enqueue happened. Callers key their dedup on that return, so a swallowed
    failure is retried rather than remembered as done.

    The single write path, so the per-skill call and any future batch caller
    cannot drift in what they record. The unified emitter (`shared.telemetry`)
    owns persistence: the batch lands in the `events` table with the legacy
    `event_log` mirror, and the emitter's JSONL mirror is the durable fallback
    — the enqueue is a bounded-queue put, not a DB round-trip per skill.

    Best-effort: emit never raises (attribution is telemetry; it must never
    take an agent down).

    Honest contract: `True` means "enqueued", not "persisted" — the write is
    async (drain thread), drained at process exit via the emitter's atexit
    hook (exec subprocesses included); a SIGKILL still loses the DB copy
    (JSONL mirror + file sinks hold the line). Dedup marks "enqueued".
    """
    if not skills:
        return True
    try:
        from shared.audit_events import insert_event_log_many

        insert_event_log_many(
            event_type="skill_invoked",
            agent_id=agent,
            source="self",
            payloads=[
                SkillInvokedPayload(
                    skill=skill["name"],
                    identifier=identifier(skill),
                    invocation_depth="loaded",
                ).model_dump()
                for skill in skills
            ],
        )
    except Exception as e:
        names = ", ".join(s["name"] for s in skills)
        logger.warning("skill_invoked event write failed for {}: {}", names, e)
        return False
    return True


def _node_to_obj(path: str, node: _Leaf | _NS) -> _SkillProxy | _Namespace:
    """Wrap a tree node: a `_Leaf` → `_SkillProxy`; an `_NS` → `_Namespace`.

    Resolution is exposure, not use: no SKILL.md body is read here and no
    `skill_invoked` attribution is recorded. Index renders and system-prompt
    assembly resolve nodes every turn (assembly walks `_flatten` directly) —
    recording here made exposure look like use: ~70% of "loaded" rows had no
    usage trace in the transcript. Attribution fires on first body
    consumption, in the lazy `__doc__` loaders (`_LazySkillDoc`)."""
    if isinstance(node, _Leaf):
        return _SkillProxy(path, node.skill)
    return _Namespace(path, node)


def __getattr__(name: str) -> _SkillProxy | _Namespace:
    """Module `__getattr__` hook: `ava.skills.<attr>` lazily returns a skill
    proxy (leaf) or a namespace node whose own attrs descend the tree.

    Raises:
        AttributeError: no such top-level skill or namespace (so
            `hasattr(ava.skills, 'x')` returns False rather than throwing).
    """
    if name.startswith("_"):
        raise AttributeError(name)
    tree = _scan_tree()
    if name not in tree:
        raise AttributeError(f"ava.skills has no {name!r} (available: {sorted(tree)})")
    return _node_to_obj(name, tree[name])


def __dir__() -> list[str]:
    """`dir(ava.skills)` returns top-level skill attrs + namespace names +
    public utility methods (`read`).

    Parse failures propagate (fail-fast); don't silently hide broken skills.
    """
    return sorted({*_scan_tree(), "read"})


class _SkillsModule(types.ModuleType):
    """The class of the `ava.skills` module object itself.

    Its only job is to carry `__all_for_ava__` as a property. That name is the
    curated agent-visible surface every `ava.help()` render and the SDK-expand
    discovery read through `ava.agent_visible_names`, and it must be computed
    from the live skill tree — but `agent_visible_names` reads it with
    `getattr_static`, which sees class attributes and never a PEP 562 module
    `__getattr__`. A property on the module's own class is the same shape
    `ava.mcps._ServerProxy` uses for its tool list. Without it the render falls
    back to walking `dir()`, which is uncurated by construction: whatever the
    module happens to expose, filtered by heuristics rather than declared.
    """

    @property
    def __all_for_ava__(self) -> list[str]:
        """Top-level skill and namespace names plus the `read` utility — the
        index a bare `ava.help(ava.skills)` renders, one heading + one-line
        description per entry, and the `read` function's contract. Bodies
        never render from here; `ava.help(ava.skills.<name>)` /
        `ava.skills.read(<name>)` open one."""
        return sorted({*_scan_tree(), "read"})


sys.modules[__name__].__class__ = _SkillsModule
