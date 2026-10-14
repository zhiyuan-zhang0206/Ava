"""Scanning skill roots into the namespace tree.

`SkillIndexBuilder` mounts roots; `scan_tree` and `skills_in` are functions of the roots, the
install-registry gate and the provider roots they are given, so a test passes a `tmp_path`
and a set of names. The facade `ava.skills` supplies the real ones.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from base.log import logger
from base.packages.skills.index import SkillFile, SkillIndex
from base.packages.skills.names import SkillIdentity, match_key
from base.packages.skills.tree import NS, Leaf, Skill, claim, descend, flatten

__all__ = ["SkillIndexBuilder", "scan_tree", "skills_in"]


class SkillIndexBuilder:
    """Builds the skill namespace tree from mount roots.

    Owns the state the mount threads through — the tree, the per-root
    claimed-paths registry, and the cross-root seen-hashes dedup set — as
    instance state. The folder scan + frontmatter parse itself lives in
    `base.packages.skills.index` (doorplate ⑤): one builder behind every skill read
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
        *root skill* (its `NS` carries `.skill`); an `INDEX.md` sets that
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
        # The scan is `base.packages.skills.index` (doorplate ⑤), mtime-cached: this
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
        parent = descend(self.tree, ns)
        key = match_key(skill["name"])
        self._claim_paths(root, rel, key, str(skill_md.parent))
        existing = parent.get(key)
        if isinstance(existing, NS):
            existing.skill = skill  # children mounted first — this folder is a root skill
        else:
            parent[key] = Leaf(skill)
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
        descend(self.tree, self._base_ns + rel).doc = entry.index_text

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
            claim(self._claimed, attr_path, str(root.joinpath(*rel[: depth + 1])))
        claim(self._claimed, (*attr_path, key), src)

    def _maybe_promote(self, parent: NS, key: str, skill: Skill, ns: tuple[str, ...]) -> None:
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


def scan_tree(
    skills_root: Path, gate: set[str] | None, provider_roots: Sequence[Path]
) -> dict[str, Any]:
    """Build the full namespace tree: `skills_root` (`~/.ava/skills/`, gated by the install
    registry's loadable names) plus provider roots (scanned last, so a project-local skill
    overrides a same-named converged one).

    A shared `seen_hashes` set tracks SHA256 hashes of every SKILL.md content
    loaded across all mount points, preventing duplicate entries when the same
    skill file content appears at multiple paths (e.g. `.claude/skills/`,
    `.agents/skills/` and `.ava/skills/` in the same repo, where two of the
    three are usually links back to the third).
    """
    builder = SkillIndexBuilder()
    builder.mount(skills_root, gate)
    for root in provider_roots:
        builder.mount(root, None)
    return builder.tree


def skills_in(roots: list[Path]) -> list[Skill]:
    """Scan the given roots (mounted bare at the tree root, no overlay gating)
    and return their skills, sorted. Used for project-local provider roots.

    Same parse + fail-fast semantics as the full scan. A shared `seen_hashes`
    set prevents duplicate entries across the given roots.
    """
    builder = SkillIndexBuilder()
    for root in roots:
        builder.mount(root, None)
    return flatten(builder.tree)
