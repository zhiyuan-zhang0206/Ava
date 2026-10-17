"""Reusable instruction packs you read and follow."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

from base.packages.skills.tree import Skill, flatten, identifier
from base.paths import meiva_home, skills_dir
from meiva.sdk_surface import process_context, skill_sources
from meiva.skills.skill_proxies import Namespace, SkillProxy, node_to_obj

# Attribution timing: a `skill_invoked` row at depth `"loaded"` — the only
# depth the producer writes — means the agent CONSUMED a skill's SKILL.md body
# (a `help()` render, a direct `__doc__` read, or an `meiva.files.read` of the
# skill's SKILL.md — see `record_skill_invoked_by_path`). Resolving a node
# (`meiva.skills.<name>` access) is exposure, not use: the system-prompt index
# and every index render walk the same tree every turn and read only
# frontmatter descriptions, so they must record nothing. `_SkillProxy` and
# `_Namespace` therefore load their SKILL.md body lazily on first `__doc__`
# access and record the attribution there. `"loaded"` is the only depth
# meiva_self_evolution scores
# (`meiva_builtins/skills/meiva_self_evolution/reference/collect.py:_skills_touched`);
# baseline prompt exposure is not recorded at all.


# Skills load as a **namespace tree mirroring folder structure**: every SKILL.md
# is a leaf node, and the folders above it (under its mount point) are its
# namespace path. Mount points:
#   - ~/.meiva/skills/                          → tree root (install-registry gated)
#   - provider roots (a plugin's declared skill_sources)  → tree root (runtime, project-local)
#
# `~/.meiva/skills/` is THE load dir: repo skills (`<repo>/meiva_builtins/skills/<group>/*`) and plugin
# skills (`<repo>/meiva_builtins/plugins/<p>/skills/*`, `~/.meiva/plugins/<p>/skills/*`) are
# synced into it by the skills converge step (`cli/commands/extensions/skills_sync.py`,
# run by `meiva start` / the fleet update / `meiva converge`); they are not mounted from
# their source trees. A plugin's skills sync under its name
# (`~/.meiva/skills/<p>/…`), so the namespace layer survives as plain folder
# structure. The agent never sees "plugins" — only a skill tree whose shape is
# the folder shape.
#
# So `~/.meiva/skills/meiva-goal/SKILL.md` → meiva.skills.meiva_goal (bare);
# `~/.meiva/skills/coding/tdd/SKILL.md` → meiva.skills.coding.tdd;
# `~/.meiva/skills/superpowers/brainstorming/SKILL.md` →
# meiva.skills.superpowers.brainstorming.
#
# A folder is, like a Python package, both a thing and a container:
#   - SKILL.md, no skill-bearing children   → a plain leaf skill (a module).
#   - SKILL.md AND skill-bearing children    → a *root skill* (a package whose
#     `__init__` has a body): the folder is invocable AND a namespace. `meiva.help`
#     on it renders its own SKILL.md, then lists its children.
#   - INDEX.md, no SKILL.md                   → a namespace labelled by an authored
#     description (a package `__init__` with only a docstring). Optional — a bare
#     folder with neither just synthesizes a "contains: …" line.
# A folder never needs both SKILL.md and INDEX.md (SKILL.md's description is the
# label); if both exist the SKILL.md wins.
#
# Two renderings of a skill's location, and one fold between them
# (`base/packages/skills/names.py` — dash is canonical outward, underscore is the Python
# projection inward):
#   - composer / display identifier — `.`-joined dash form:
#     superpowers.brainstorming, coding.tdd, or bare `goal` (see `identifier`).
#   - Python access path — `.`-joined attr form (hyphens→underscores):
#     meiva.skills.superpowers.test_driven_development (see `target`).
# Every dash/underscore comparison in this module goes through `match_key`, so a
# legacy underscore directory and its dash spelling are one name — which is also
# why two directories that fold together are refused (`SkillNameCollision`).
#
# Loading from `~/.meiva/skills/` is gated by the install registry
# (loadable_skill_names): a top-level directory with no enabled registry entry —
# hand-copied, or tracked but disabled — is silently ignored (register with
# `meiva skill register <name>`), and an enabled package whose manifest excludes
# this host (`engines.meiva` / `requires_commit`) is dropped too, with the reason
# visible in `meiva packages status`. Keeps stray copies from silently loading.
# Provider roots are scanned last (override). The scan is the shared,
# mtime-cached SkillIndex (doorplate ⑤) — see base/packages/skills/index.py.
# Frontmatter requires name + description; other files in a skill dir are read
# by the agent via meiva.files.read.


# Plugin-contributed skill-root providers: the installed list lives in
# `meiva.sdk_surface.skill_sources` (framework-internal, written only by `meiva.sdk_surface.install`), off
# this disable-able `meiva.skills` module.


def _provider_roots() -> list[Path]:
    """Flatten all registered providers' roots into one list (scan order)."""
    return skill_sources.roots()


def _scan_tree() -> dict[str, Any]:
    """The full namespace tree: `~/.meiva/skills/` (install-registry gated) plus the provider
    roots; the scan itself is `skill_index.scan_tree`, which takes all three as arguments."""
    from base.packages.extensions.install_registry import loadable_skill_names
    from meiva.skills.skill_index import scan_tree

    return scan_tree(skills_dir(meiva_home()), loadable_skill_names(), _provider_roots())


def skills_in(roots: list[Path]) -> list[Skill]:
    """Scan the given roots (mounted bare at the tree root, no overlay gating)
    and return their skills, sorted. Used for project-local provider roots.

    Same parse + fail-fast semantics as the full scan. A shared `seen_hashes`
    set prevents duplicate entries across the given roots.
    """
    from meiva.skills.skill_index import skills_in as scan_roots

    return scan_roots(roots)


def names() -> list[Skill]:
    return flatten(_scan_tree())


def _consume_skill_body(skill: Skill) -> str:
    """Read one skill's SKILL.md body, record the `skill_invoked` attribution,
    and return the consumed shape — a path line (tilde-shortened) then the raw
    body, the shape a proxy's `__doc__` carries. The one body-consumption
    point behind every proxy `__doc__` and `meiva.help` render, so each records
    identically."""
    from pathlib import Path as _Path

    info = skill
    content = (_Path(info["path"]) / "SKILL.md").read_text(encoding="utf-8")
    _record_skill_invoked(info)
    home = str(_Path.home())
    short = f"~{info['path'][len(home) :]}" if info["path"].startswith(home + "/") else info["path"]
    return f"{short}\n\n{content}"


def record_skill_invoked_by_path(path: str | Path) -> bool:
    """Record the `skill_invoked` attribution for a direct SKILL.md file read.

    The `.path` + `meiva.files.read(SKILL.md)` pattern consumes a skill body like
    a `help()` render, but never touches the lazy proxy hook that carries the
    attribution — so it used to record nothing and under-counted `loaded`
    (facility-wide scan: 68 agents / 3 days used the pattern, 13 with zero
    rows). `meiva.files.read` routes here. Only a loaded skill's own SKILL.md
    qualifies (a SKILL.md outside the mount roots is not a skill the agent
    loaded); best-effort end to end, so this never raises. Returns whether a
    matching skill was found and attributed."""
    p = Path(path).expanduser().absolute()
    if p.name != "SKILL.md":
        return False
    for skill in names():
        if Path(skill["path"]).expanduser().absolute() == p.parent:
            _record_skill_invoked(skill)
            return True
    return False


def _note_skill_read(skill_identifier: str) -> None:
    """Hand the identifier of a skill whose body was just read to the exec turn's state update,
    so the agent host knows which skills are already in this context window (skill recall
    does not offer them again).

    The same single point as the `skill_invoked` audit, so every way of reading a body is
    covered. Plain strings only: this runs in the exec child, which must not import the
    agent state classes or the model stack. Outside an exec turn there is no state update to
    carry it and the read is not noted."""

    if not process_context.in_exec_turn():
        return
    update = process_context.get_state_update()
    if not isinstance(update, dict):
        return  # agent code replaced the slot; the exec child reports that at exit
    reads: list[str] = update.setdefault("skill_reads", [])
    if skill_identifier not in reads:
        reads.append(skill_identifier)


def _record_skill_invoked(skill: Skill) -> None:
    """Record that this agent opened this skill: one `skill_invoked` audit event
    at the `"loaded"` depth (the only depth the producer writes), written at the
    moment of consumption through the audit-event path (`audit_events` row in
    Postgres, then the unified stream). Nothing is buffered or deduplicated in
    this process: every body consumption is one event, and the consumer
    (`meiva_self_evolution`'s `_skills_touched`) reads the set of skills, not row
    counts. Skipped outside an agent process (no identity to attribute to).

    Attribution must never take an agent down, so the write is the *reported*
    kind: a failed write logs an error with its traceback and emits an
    `audit_write_failed` anomaly event instead of raising.
    """
    from meiva.sdk_surface.agent_identity import require_agent_id

    context = process_context.ambient_context()
    try:
        agent = require_agent_id(context)
    except RuntimeError:
        return
    _note_skill_read(identifier(skill))

    # Function-local: meiva.sdk_surface.settings is the composition root and pulls the shell stack; only
    # recording needs it, and the gateway's import closure must not (test_gateway_consumer_guard).
    from base.telemetry.audit_events import (
        SkillInvokedPayload,
        prepare_event_log,
        record_audit_reported,
    )
    from meiva.sdk_surface.settings import database

    capture_owner = None if context is None else context.sdk_capture
    record_audit_reported(
        database(),
        prepare_event_log(
            event_type="skill_invoked",
            agent_id=agent,
            source="self",
            payload=SkillInvokedPayload(
                skill=skill["name"],
                identifier=identifier(skill),
                invocation_depth="loaded",
            ).model_dump(),
        ),
        **({"capture": capture_owner.capture_local_event} if capture_owner is not None else {}),
    )


def __getattr__(name: str) -> SkillProxy | Namespace:
    """Module `__getattr__` hook: `meiva.skills.<attr>` lazily returns a skill
    proxy (leaf) or a namespace node whose own attrs descend the tree.

    Raises:
        AttributeError: no such top-level skill or namespace (so
            `hasattr(meiva.skills, 'x')` returns False rather than throwing).
    """
    if name.startswith("_") or name == "skill_index":
        # `skill_index` is the lazily imported scan module: the import system asks for it here
        # before loading it, and answering would scan the tree for nothing.
        raise AttributeError(name)
    tree = _scan_tree()
    if name not in tree:
        raise AttributeError(f"meiva.skills has no {name!r} (available: {sorted(tree)})")
    return node_to_obj(name, tree[name], _consume_skill_body)


def __dir__() -> list[str]:
    """`dir(meiva.skills)` returns top-level skill attrs + namespace names.

    Parse failures propagate (fail-fast); don't silently hide broken skills.
    """
    return sorted(_scan_tree())


class _SkillsModule(types.ModuleType):
    """The class of the `meiva.skills` module object itself.

    Its only job is to carry `__all_for_meiva__` as a property. That name is the
    curated agent-visible surface every `meiva.help()` render and the SDK-expand
    discovery read through `meiva.agent_visible_names`, and it must be computed
    from the live skill tree — but `agent_visible_names` reads it with
    `getattr_static`, which sees class attributes and never a PEP 562 module
    `__getattr__`. A property on the module's own class is the same shape
    `meiva.mcps._ServerProxy` uses for its tool list. Without it the render falls
    back to walking `dir()`, which is uncurated by construction: whatever the
    module happens to expose, filtered by heuristics rather than declared.
    """

    @property
    def __all_for_meiva__(self) -> list[str]:
        """Top-level skill and namespace names — the index a bare
        `meiva.help(meiva.skills)` renders, one heading + one-line description per
        entry. Bodies never render from here; `meiva.help(meiva.skills.<name>)`
        opens one."""
        return sorted(_scan_tree())


sys.modules[__name__].__class__ = _SkillsModule
