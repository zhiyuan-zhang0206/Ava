"""The `# Capabilities` index — the system prompt's one listing of what this
agent already has (skills + live MCP tool servers).

Split out of `_system_prompt.py` so the prompt-assembly module stays inside the
per-file line budget. The section function is registered by `_system_prompt`
(rather than decorated here) so the render order stays where the reading order
puts it — Capabilities last among the framework-owned sections — without this
module having to import back into its own importer.

Also home to the *capability surface* concept (`ava.skills` / `ava.mcps`): the
expanded SDK reference consults it to avoid rendering a second index of the
same thing.
"""

import logging
from typing import Any, NamedTuple

from shared.config.turn_view import turn_settings
from shared.skill_names import match_key

# A description is free-form text from a SKILL.md frontmatter block — including
# ones dropped into `~/.ava/skills/` by a user or a plugin. A YAML block scalar
# can carry newlines and markdown, which would let one skill's description
# inject headings, list items, or instructions into the index it is listed in.
# One index line is one line.
_DESC_MAX_CHARS = 300


def _disabled_by_sdk_config(path: str) -> bool:
    """True when `path` or one of its dotted parents is SDK-disabled.

    Two disable sources, one rendering rule. `turn_settings.agent.sdk_disable`
    carries the env (`AVA_SDK_DISABLE`) and per-agent overlay entries; the
    eval-isolation boundary (`_apply_per_agent_eval_isolation`) removes
    surfaces at runtime via `ava._apply_sdk_disable` without touching
    settings. Checked before any resolution attempt: the operator removed the
    path from the SDK on purpose, so it must never be expanded into the prompt
    regardless of what still resolves — so the live namespace is consulted too,
    one segment at a time: a segment that no longer resolves means the path is
    gone and nothing renders it."""
    if any(
        path == entry or path.startswith(entry + ".") for entry in turn_settings.agent.sdk_disable
    ):
        return True
    import ava

    node: object = ava
    for segment in path.split("."):
        node = getattr(node, segment, None)
        if node is None:
            return True
    return False


# Namespaces `"*"` never expands. `ava.skills` and `ava.mcps` are *capability
# surfaces*, not SDK API: their agent-visible members are the installed skills
# and the configured MCP servers, so expanding them renders a second, full index
# of exactly what `# Capabilities` already indexes. One index, one place — the
# Capabilities section owns capability discovery, the expanded SDK reference owns
# call contracts. Naming a surface itself explicitly in AVA_SDK_EXPAND still
# expands it (an operator override — the render is index-style, so it costs a
# duplicate index and nothing worse), and the SDK overview still lists both as
# namespaces, so `ava.help(ava.skills)` remains one call away.
_CAPABILITY_SURFACES = frozenset({"skills", "mcps"})


def _is_capability_surface_member(path: str) -> bool:
    """True for a path *inside* a capability surface (`skills.gmail`) — not the
    surface itself, which an operator may still expand.

    Refused as a correctness guard, not a budget one: resolving it walks plain
    `getattr` to ONE skill / server, the same path an agent access takes, so it
    inlines that skill's whole SKILL.md (or that server's tool schemas) into
    every prompt AND records a "loaded" attribution nobody earned. A skill body
    belongs in `skills_to_expand_at_start`, which is accounted for as cost.
    """
    head, _, rest = path.partition(".")
    return bool(rest) and head in _CAPABILITY_SURFACES


# (config_field, name) pairs already warned about in this process. A configured
# name that resolves to nothing is a fact about static config, so it is worth
# saying once and worth nothing after that — and resolution is no longer a
# per-window event: the drift check re-resolves before every LLM call, which
# without this would repeat the same warning for an agent's whole life.
_warned_unresolved: set[tuple[str, str]] = set()


def _warn_unresolved_once(config_field: str, name: str) -> None:
    """Warn that a configured skill name matched nothing — at most once per
    (list, name) per process. Naming `config_field` so the operator sees which
    list is stale."""
    if (config_field, name) in _warned_unresolved:
        return
    _warned_unresolved.add((config_field, name))
    logging.getLogger(__name__).warning(
        "%s: skill %r not found among loaded skills, skipping", config_field, name
    )


def resolve_prompt_skills(wanted: list[str], *, config_field: str) -> list[Any]:
    """Resolve a config list of skill names to loaded `ava.skills.Skill` entries
    — the one resolver shared by the capabilities index (name + one-line
    description) and the preloaded-skills note (full SKILL.md body), so both
    agree on what a name means and what "not found" does. Typed `list[Any]`
    rather than `list[Skill]` because `ava.skills` is lazily imported (SDK-disable
    + boot ordering keep it out of module scope, and TYPE_CHECKING is banned).

    `*` selects the whole loaded catalog. Otherwise each name resolves by
    `.`-identifier first (unambiguous for namespaced skills like `ava-code.pr`),
    then bare frontmatter name for flat entries. Both sides of the match go
    through `shared.skill_names.match_key`, so a stored value still spelled
    `ava_code.pr` (a preset row written before the dash rename, an operator
    typing the Python form) resolves to the same skill as `ava-code.pr`. An
    unresolved name warns once per process (naming `config_field` so the operator
    sees which list is stale) and is skipped — a plugin-bundled skill only
    resolves while its plugin is enabled, and skills differ per machine.

    Returns `[]` when the `skills` SDK surface is disabled, nothing is
    configured, or nothing resolves.
    """
    if _disabled_by_sdk_config("skills"):
        return []
    if not wanted:
        return []

    import ava

    loaded = ava.skills._names()
    if "*" in wanted:
        # Wildcard: select the whole catalog — a human asked one agent to see
        # every loaded skill, so skip per-name resolution entirely.
        skills = loaded
    else:
        by_ident = {match_key(ava.skills.identifier(s)): s for s in loaded}
        by_name = {match_key(s["name"]): s for s in loaded}
        skills: list[Any] = []
        for name in wanted:
            key = match_key(name)
            skill = by_ident.get(key) or by_name.get(key)
            if skill is None:
                _warn_unresolved_once(config_field, name)
                continue
            skills.append(skill)  # pyright: ignore[reportUnknownArgumentType]

    return skills


def _one_line(description: str) -> str:
    """Flatten a skill description to a single index line. See `_DESC_MAX_CHARS`
    — an index entry is untrusted text from whoever wrote the SKILL.md, and the
    index's whole contract with the agent is one line per capability."""
    flat = " ".join(description.split())
    return flat if len(flat) <= _DESC_MAX_CHARS else flat[: _DESC_MAX_CHARS - 1] + "…"


def indexed_skills() -> list[Any]:
    """The skills `# Capabilities` covers, resolved against the catalog as it is
    **right now**.

    `ava.skills._names()` is an uncached filesystem scan, so this is a live
    answer; the rendered index is one frozen sample of it, taken when
    `init_context` builds the SystemMessage. Everything that has to reason about
    the distance between the two — the snapshot `init_context` records, the drift
    check that keeps it honest (`agent/hooks/capabilities.py`) — comes through
    here, so no caller re-derives which config list decides membership.

    Resolution (wildcard, identifier-then-name, warn-and-skip) is shared with the
    preloaded-skills note via `resolve_prompt_skills`. Empty when nothing is
    configured, `skills` is SDK-disabled, or nothing resolves."""
    return resolve_prompt_skills(
        turn_settings.agent.skills_to_inject_into_system_prompt,
        config_field="skills_to_inject_into_system_prompt",
    )


def _index_line(skill: Any) -> str:
    """One index entry. Display form — `ava.skills.<dash:colon>` (e.g.
    `ava.skills.web-ai:deep-research`), the same spelling the expanded-SDK
    section uses; the loadable form is that path with `-`/`:` -> `_`/`.`.

    The description is untrusted frontmatter from whoever wrote the SKILL.md
    (audit round-2 up-security-trust P0-2): a flagged description is not
    quoted into the system prompt — the line renders as a security marker, so
    an 'ignore previous instructions' imperative inside 300 chars cannot enter
    the index. (The runtime mount gate already refuses critical-flagged
    skills entirely; this is the in-depth check for patterns the skill-scan
    table does not carry.)"""
    import ava
    from ava.security import is_flagged

    description = skill["description"]
    if is_flagged(description):
        identifier = ava.skills.identifier(skill)
        return (
            f"- `ava.skills.{identifier}` — [security-flagged] description withheld "
            f"(load the body with `ava.help(ava.skills.{identifier})` and judge it yourself)"
        )
    return f"- `ava.skills.{ava.skills.identifier(skill)}` — {_one_line(description)}"


def _skill_index_lines() -> list[str]:
    """The skills half of the capabilities index: one `ava.skills.<path>` +
    one-line-description entry per injected skill."""
    return [_index_line(s) for s in indexed_skills()]


def indexed_skill_identifiers() -> set[str]:
    """Display identifiers of `indexed_skills()` — the key a snapshot of the
    index is taken on, and the same string an index line names."""
    import ava

    return {ava.skills.identifier(s) for s in indexed_skills()}


class IndexDrift(NamedTuple):
    """What the capability index covers now, against a snapshot of what it
    covered when it was last rendered.

    `identifiers` is the whole current membership — the value a caller writes
    back as the new snapshot. `added` are the entries absent from the snapshot,
    in index order, as full `Skill` records because naming one to the agent
    needs its description, not just its identifier.
    """

    identifiers: set[str]
    added: list[Any]


def index_drift(known: set[str]) -> IndexDrift:
    """Diff the live index membership against `known`, in one scan.

    One scan matters: `identifiers` and `added` have to describe the same moment,
    or a caller advancing the snapshot from one scan while announcing additions
    from another can mark a skill known without ever naming it.

    Only additions are reported. A skill that disappears leaves the standing
    index over-promising until the next build, which `ava.skills.<name>` already
    fails fast on; dropping it from `identifiers` is enough, and makes a
    reinstall announce itself again."""
    live = indexed_skills()
    import ava

    return IndexDrift(
        identifiers={ava.skills.identifier(s) for s in live},
        added=[s for s in live if ava.skills.identifier(s) not in known],
    )


# Agent-visible framing for the drift note. Says what the lines are and what
# they are relative to — the standing `# Capabilities` section, which the agent
# has already read and which does not mention these.
_NEW_SKILLS_FRAMING = (
    "Skills installed since your `# Capabilities` index was built. That index is "
    "a snapshot taken at the start of this context window; these are additions "
    "to it. Same contract as the lines there — a one-line summary each, never "
    "enough to act on: load the full body with `ava.help(ava.skills.<path>)` "
    "before using one."
)


# One install is normally one skill. A first converge on a fresh box, a plugin
# sync landing a whole pack, or an operator unpacking a bundle is one drift event
# carrying dozens — and the note would then spend the window on a second full
# index nobody asked for. Cap the listing and point at the catalog for the rest.
# The snapshot still advances over ALL of them (membership is replaced whole), so
# the unlisted ones are never named later; `ava.help(ava.skills)` and the next
# compaction's rebuilt index are what cover them.
_NEW_SKILLS_MAX_ENTRIES = 20


def new_skills_note_text(skills: list[Any]) -> str:
    """The drift note's body — the framing plus one index line per skill, in the
    exact shape `# Capabilities` uses, so the agent reads it as more of the same
    listing rather than a different kind of thing. Capped at
    `_NEW_SKILLS_MAX_ENTRIES` lines with a counted tail."""
    listed = skills[:_NEW_SKILLS_MAX_ENTRIES]
    lines = [_index_line(s) for s in listed]
    if len(skills) > len(listed):
        lines.append(
            f"- … and {len(skills) - len(listed)} more — "
            "`ava.help(ava.skills)` enumerates the full catalog"
        )
    return _NEW_SKILLS_FRAMING + "\n\n" + "\n".join(lines)


def _mcp_index_lines() -> list[str]:
    """The MCP half of the capabilities index: one `ava.mcps.<server>` entry per
    configured server, carrying its config `description` when set (name only
    otherwise). Empty when no server is configured."""
    if _disabled_by_sdk_config("mcps"):
        return []
    import ava

    lines: list[str] = []
    for server in ava.mcps.servers():
        desc = ava.mcps.description(server)
        lines.append(
            f"- `ava.mcps.{server}` — {_one_line(desc)}" if desc else f"- `ava.mcps.{server}`"
        )
    return lines


# Header prose, assembled from whichever halves actually rendered. Promising an
# agent a skill index it does not have (AVA_SDK_DISABLE=skills, an empty inject
# list, a cluster with no skills converged yet) points it at a listing that is
# not there — and the section still renders whenever the OTHER half has entries.
_WHAT_SKILLS = "reusable skills (playbooks)"
_WHAT_MCPS = "live MCP tool servers"

_SKILLS_HOWTO = (
    "Before using a skill, load its full body with `ava.help(ava.skills.<path>)`; "
    "that also prints the skill's directory path, where its other files live. "
    "Paths nest like `ava.skills.web-ai:deep-research` — display form; the "
    "loadable spelling of the same path is `ava.skills.web_ai.deep_research`. "
    "This listing may be a subset of what is installed — "
    "`ava.help(ava.skills)` enumerates the full catalog, and an unlisted skill "
    "is still reachable by name."
)

_MCP_HOWTO = (
    "`ava.mcps.servers()` lists every configured server and "
    "`ava.help(ava.mcps.<server>)` lists one server's tools."
)

_MATCH_EVERY_TASK = (
    "This is your index of what you can already do. Match every task "
    "against it before you start — that is the only thing standing between "
    "you and rebuilding something (email, browser, transcription, feeds, "
    "...) that already exists here."
)


def capability_index_is_empty() -> bool:
    """True when `capabilities_section()` would render nothing — no skill lines
    and no MCP servers. The delegation check reads this to decide whether it can
    point at a `# Capabilities` section: with `skills` SDK-disabled, an empty
    inject list, or a cluster where nothing is converged yet, the section is
    absent and a step ordering the agent to read it is an instruction to nowhere.

    Resolves the same two halves the section does. The skill half's attribution
    write is dedup'd per (agent, skill, depth), so the section's own resolve a
    few sections later is a no-op rather than a second write."""
    return not _skill_index_lines() and not _mcp_index_lines()


def capabilities_section() -> str:
    """Always-on index of capabilities this agent already has — skills (reusable
    playbooks) and live MCP tool servers — so a concrete task starts from what
    exists instead of being rebuilt from general knowledge. Names + one-line
    descriptions only; full bodies / tool lists load on demand. This is the
    prompt's ONE skill index (the expanded SDK reference deliberately skips the
    capability surfaces — see `_CAPABILITY_SURFACES`), and the delegation check
    is what makes an agent actually read it. No skills configured and no MCP
    servers -> no section.

    Registered by `_system_prompt` rather than decorated here — see the module
    docstring."""
    skill_lines = _skill_index_lines()
    mcp_lines = _mcp_index_lines()
    if not skill_lines and not mcp_lines:
        return ""

    what = " and ".join(
        w for w, present in ((_WHAT_SKILLS, skill_lines), (_WHAT_MCPS, mcp_lines)) if present
    )
    intro = [
        # Sentence-case the assembled phrase by hand: str.capitalize() would
        # lowercase the rest of it, and "MCP" has to stay shouting.
        f"{what[0].upper()}{what[1:]} you already have. "
        "Each line is only a one-line summary — never act on it alone."
    ]
    if skill_lines:
        intro.append(_SKILLS_HOWTO)
    if mcp_lines:
        intro.append(_MCP_HOWTO)

    parts = ["# Capabilities\n\n" + " ".join(intro) + "\n\n" + _MATCH_EVERY_TASK]
    if skill_lines:
        parts.append("**Skills** — reusable playbooks:\n" + "\n".join(skill_lines))
    if mcp_lines:
        parts.append(
            "**MCP servers** — live tools, call `ava.mcps.<server>.<tool>(...)`:\n"
            + "\n".join(mcp_lines)
        )
    return "\n\n".join(parts)
