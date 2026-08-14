"""Installed-skills panel — GET /api/skills, PUT /api/skills.

A single-host, gateway-local read: this host's `$AVA_HOME/skills/` load dir
(the only directory the skill scanner loads) correlated with the install
registry (`installed.json`). Skills are per-machine — the registry is
machine-local, there is no cluster-shared row — so unlike the cross-machine
plugin/MCP inventory this is not a `?machine=` matrix; it reports what this
gateway host itself carries.

GET lists all installed skills. PUT toggles one skill's enabled flag in the
install registry — the change is immediate and the skill scanner picks it up on
the next agent spawn.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from gateway.schemas import SkillEnableUpdate, SkillsView, SkillView
from gateway.schemas.skills import SkillLayer
from shared import install_registry, paths
from shared.install_registry import IGNORED_NAMES, PackageOrigin, tree_hash
from shared.skill_names import display_name, match_key

router = APIRouter()

# Registry origin -> the layer surfaced to the panel.
_LAYER: dict[PackageOrigin, SkillLayer] = {
    "repo": "core",
    "plugin": "plugin",
    "user": "machine",
}


def _build_skill_view(
    name: str, entry: install_registry.InstalledPackage | None, dir_path: Path
) -> SkillView:
    """Build a SkillView for one skill directory, correlated with its registry entry."""
    if entry is None:
        return SkillView(
            name=display_name(name),
            layer="untracked",
            enabled=False,
            modified_locally=False,
        )
    # Drift is only defined for a converge-managed copy (content_hash
    # set); machine/user entries carry no managed hash → never "drifted".
    # Marker-protected local subtrees are excluded, so a package
    # carrying local adapters does not read as permanently drifted.
    drifted = (
        entry.content_hash is not None
        and tree_hash(dir_path, skip_subtrees=install_registry.preserved_subpaths(dir_path))
        != entry.content_hash
    )
    return SkillView(
        name=display_name(name),
        layer=_LAYER[entry.origin],
        enabled=entry.enabled,
        modified_locally=drifted,
        origin_path=entry.origin_path,
    )


@router.get("/api/skills")
def get_skills() -> SkillsView:
    """List the skills in this host's load dir, correlated with the registry.

    Each top-level directory under `$AVA_HOME/skills/` becomes a row: its source
    layer (core=repo / plugin / machine=user), its `enabled` toggle, and whether
    the on-disk copy drifted from what converge last wrote (`modified_locally`).
    A directory with no registry entry is reported as `untracked` (present on
    disk but the scanner won't load it)."""
    registry = install_registry.load()
    # Registry rows are keyed by folded name (design R2-B1): a row written
    # `ava-code` and a directory still spelled `ava_code/` are one skill —
    # correlate through the fold, not the raw spelling (audit 02 #4).
    by_name = {match_key(p.name): p for p in registry.packages}
    skills_root = paths.skills_dir()

    rows: list[SkillView] = []
    if skills_root.is_dir():
        for d in sorted(skills_root.iterdir()):
            if not d.is_dir() or d.name in IGNORED_NAMES or d.name.startswith("."):
                # dot-prefixed dirs are converge's own temp/trash staging (and
                # never registry-tracked), so they are not panel rows
                continue
            entry = by_name.get(match_key(d.name))
            rows.append(_build_skill_view(d.name, entry, d))
    return SkillsView(skills=rows)


@router.put("/api/skills")
def update_skill_enabled(body: SkillEnableUpdate) -> SkillView:
    """Toggle one skill's enabled flag in the install registry.

    The change is persisted to `installed.json` immediately; the skill scanner
    picks it up on the next agent spawn. Untracked skills (no registry entry)
    cannot be toggled — register them first with `ava skill register`.

    `body.name` is an inbound name: the lookup folds dash and underscore
    together (`match_key`), and the row's own spelling — not the request's — is
    what addresses the directory and comes back in the view.

    Read and write are ONE `mutate` cycle. Looking the row up through a separate
    read and then re-saving it would let a concurrent `ava skill install` or
    `ava converge` land its rows in between, and this handler's save — built on
    a registry read before those rows existed — would drop them."""
    with install_registry.mutate() as registry:
        key = match_key(body.name)
        pkg = next((p for p in registry.packages if match_key(p.name) == key), None)
        if pkg is None:
            raise HTTPException(
                status_code=404,
                detail=f"Skill '{body.name}' not found in registry — register it first with `ava skill register`",
            )
        pkg.enabled = body.enabled

    # Build the updated SkillView from the on-disk directory
    dir_path = paths.skills_dir() / pkg.name
    return _build_skill_view(pkg.name, pkg, dir_path)
