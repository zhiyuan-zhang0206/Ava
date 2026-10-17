# meiva:note
# The `# Capabilities` section is the system prompt's single index of available skills and live MCP servers, rendering only non-empty halves and omitting the section when both are empty. It directs the agent to match every task against the index before starting so established playbooks and domain capabilities are not skipped or rebuilt. Skill resolution shares one resolver across the index and preloaded skills, folding descendants under selected ancestor entry skills on wildcard selection while preserving full catalog membership for drift tracking and preloading. Untrusted skill descriptions are flattened and truncated to one 300-character line, and injection-flagged descriptions are withheld from the index and excluded from recall candidates along with preloaded skills. Disable checks honor configured `Prompt.sdk_disable` and runtime eval-isolation removals while leaving unconfigured missing paths eligible for expansion diagnostics. Index drift diffs live membership in one scan and caps additions at 20 lines while advancing the snapshot over full membership. Prompt assembly records no skill attribution and requests no capability report in final output.
# meiva:link agent/graph/prompt/docs/composition.meiva.okf.md
import logging
from collections.abc import Sequence
from typing import Any, NamedTuple

from base.agents.messages.injection_scan import is_flagged
from base.host.env.agent_slices import AgentSlices, Prompt
from base.packages.skills.names import match_key
from base.packages.skills.tree import identifier, target

_DESC_MAX_CHARS = 300


def disabled_by_sdk_config(path: str, sdk_disable_config: Sequence[str]) -> bool:
    if any(path == entry or path.startswith(entry + ".") for entry in sdk_disable_config):
        return True
    from meiva.sdk_surface import sdk_disable

    return any(
        path == entry or path.startswith(entry + ".") for entry in sdk_disable.applied_entries()
    )


CAPABILITY_SURFACES = frozenset({"skills", "mcps"})


def is_capability_surface_member(path: str) -> bool:
    head, _, rest = path.partition(".")
    return bool(rest) and head in CAPABILITY_SURFACES


def _warn_unresolved(config_field: str, name: str) -> None:
    logging.getLogger(__name__).warning(
        "%s: skill %r not found among loaded skills, skipping", config_field, name
    )


def resolve_prompt_skills(
    wanted: Sequence[str], sdk_disable_config: Sequence[str], *, config_field: str
) -> list[Any]:
    if disabled_by_sdk_config("skills", sdk_disable_config):
        return []
    if not wanted:
        return []

    import meiva

    loaded: list[Any] = meiva.skills.names()
    if "*" in wanted:
        skills = loaded
    else:
        by_ident = {match_key(identifier(s)): s for s in loaded}
        by_name = {match_key(s["name"]): s for s in loaded}
        skills: list[Any] = []
        for name in wanted:
            key = match_key(name)
            skill = by_ident.get(key) or by_name.get(key)
            if skill is None:
                _warn_unresolved(config_field, name)
                continue
            skills.append(skill)

    return skills


def _one_line(description: str) -> str:
    flat = " ".join(description.split())
    return flat if len(flat) <= _DESC_MAX_CHARS else flat[: _DESC_MAX_CHARS - 1] + "…"


def indexed_skills(prompt: Prompt) -> list[Any]:
    return resolve_prompt_skills(
        prompt.skills_to_inject_into_system_prompt,
        prompt.sdk_disable,
        config_field="skills_to_inject_into_system_prompt",
    )


def _index_description(skill: Any) -> str | None:
    description = skill["description"]
    return None if is_flagged(description) else _one_line(description)


def index_entry(path: str, description: str) -> str:
    return f"- `meiva.skills.{path}` — {description}"


def _index_line(skill: Any) -> str:
    path = target(skill)
    description = _index_description(skill)
    if description is None:
        return (
            f"- `meiva.skills.{path}` — [security-flagged] description withheld "
            f"(load the body with `meiva.help(meiva.skills.{path})` and judge it yourself)"
        )
    return index_entry(path, description)


class RecallCandidate(NamedTuple):
    identifier: str
    description: str
    target: str


def recall_candidates(prompt: Prompt) -> list[RecallCandidate]:
    preloaded = {
        identifier(s)
        for s in resolve_prompt_skills(
            prompt.skills_to_expand_at_start,
            prompt.sdk_disable,
            config_field="skills_to_expand_at_start",
        )
    }
    candidates: list[RecallCandidate] = []
    for skill in indexed_skills(prompt):
        ident = identifier(skill)
        description = _index_description(skill)
        if description is None or ident in preloaded:
            continue
        candidates.append(RecallCandidate(ident, description, target(skill)))
    return candidates


def _skill_index_lines(prompt: Prompt) -> list[str]:
    skills = indexed_skills(prompt)
    if "*" not in prompt.skills_to_inject_into_system_prompt:
        return [_index_line(s) for s in skills]
    by_path = {match_key(identifier(s)): s for s in skills}
    descendants: dict[str, int] = {}
    for path in by_path:
        segments = path.split(".")
        ancestors = (".".join(segments[:i]) for i in range(1, len(segments)))
        entry = next((parent for parent in ancestors if parent in by_path), path)
        descendants[entry] = descendants.get(entry, 0) + (entry != path)
    lines: list[str] = []
    for path, count in descendants.items():
        skill = by_path[path]
        line = _index_line(skill)
        if count:
            line += (
                f" ({count} sub-skills; inspect with `meiva.help(meiva.skills.{target(skill)})`)"
            )
        lines.append(line)
    return lines


def indexed_skill_identifiers(prompt: Prompt) -> set[str]:
    return {identifier(s) for s in indexed_skills(prompt)}


def skill_identifiers(skills: Sequence[Any]) -> list[str]:
    return [identifier(s) for s in skills]


class IndexDrift(NamedTuple):
    identifiers: set[str]
    added: list[Any]


def index_drift(known: set[str], prompt: Prompt) -> IndexDrift:
    live = indexed_skills(prompt)

    return IndexDrift(
        identifiers={identifier(s) for s in live},
        added=[s for s in live if identifier(s) not in known],
    )


_NEW_SKILLS_FRAMING = (
    "Additional skills are now available beyond those listed in `# Capabilities`. "
    "The descriptions below are summaries; load a skill's full instructions "
    "with `meiva.help(meiva.skills.<path>)` before using it."
)


NEW_SKILLS_MAX_ENTRIES = 20


def new_skills_note_text(skills: list[Any]) -> str:
    listed = skills[:NEW_SKILLS_MAX_ENTRIES]
    lines = [_index_line(s) for s in listed]
    if len(skills) > len(listed):
        lines.append(
            f"- … and {len(skills) - len(listed)} more — "
            "`meiva.help(meiva.skills)` enumerates the full catalog"
        )
    return _NEW_SKILLS_FRAMING + "\n\n" + "\n".join(lines)


def _mcp_index_lines(sdk_disable_config: Sequence[str]) -> list[str]:
    if disabled_by_sdk_config("mcps", sdk_disable_config):
        return []
    import meiva

    lines: list[str] = []
    for server in meiva.mcps.servers():
        desc = meiva.mcps.description(server)
        lines.append(
            f"- `meiva.mcps.{server}` — {_one_line(desc)}" if desc else f"- `meiva.mcps.{server}`"
        )
    return lines


_WHAT_SKILLS = "reusable skills (playbooks)"
_WHAT_MCPS = "live MCP tool servers"

_SKILLS_HOWTO = (
    "Before using a skill, load its full body with `meiva.help(meiva.skills.<path>)` "
    "with a path exactly as the skill list gives it, e.g. "
    "`meiva.skills.web_ai.deep_research`; the render also names the skill's "
    "directory, where its other files live. "
    "This listing may be a subset of what is installed — "
    "`meiva.help(meiva.skills)` enumerates the full catalog, and an unlisted skill "
    "is still reachable by name. Entry skills group their sub-skills: inspect "
    "the matching entry to discover specialized playbooks before choosing one."
)

_MCP_HOWTO = (
    "`meiva.mcps.servers()` lists every configured server and "
    "`meiva.help(meiva.mcps.<server>)` lists one server's tools."
)

_MATCH_EVERY_TASK = (
    "This is your index of what you can already do. Match every task "
    "against it before you start — that is the only thing standing between "
    "you and skipping an established playbook or rebuilding something "
    "(workflow, research, email, browser, transcription, feeds, ...) "
    "that already exists here."
)


def capability_index_is_empty(prompt: Prompt) -> bool:
    return not _skill_index_lines(prompt) and not _mcp_index_lines(prompt.sdk_disable)


def capabilities_section(slices: AgentSlices) -> str:
    skill_lines = _skill_index_lines(slices.prompt)
    mcp_lines = _mcp_index_lines(slices.prompt.sdk_disable)
    if not skill_lines and not mcp_lines:
        return ""

    what = " and ".join(
        w for w, present in ((_WHAT_SKILLS, skill_lines), (_WHAT_MCPS, mcp_lines)) if present
    )
    intro = [
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
            "**MCP servers** — live tools, call `meiva.mcps.<server>.<tool>(...)`:\n"
            + "\n".join(mcp_lines)
        )
    return "\n\n".join(parts)
