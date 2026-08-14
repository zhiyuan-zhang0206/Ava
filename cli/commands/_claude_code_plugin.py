"""Materialize a Claude Code plugin into Ava's models.

A Claude Code plugin ships a `.claude-plugin/plugin.json` manifest plus any of:
`skills/<name>/SKILL.md` (reusable instruction packs), `agents/*.md` (specialist
sub-agents), `commands/*.md` (slash-command prompt templates), and a root
`.mcp.json` (bundled MCP servers, declared exactly like Claude Code's). It maps
onto Ava as a single installed plugin under `<dest_root>/<name>/` that contributes:

- its **shipped skills** (when the plugin ships `skills/`): each skill directory
  is copied verbatim to `<dest_root>/<name>/skills/<skill>/`, where the skill
  scanner reads it as an external-plugin shipped skill. This is how a
  skills-only bundle (e.g. superpowers) installs.
- one **generated orchestrator skill** (when the plugin ships `agents/`): a
  `SKILL.md` that dispatches one sub-agent per specialist, with each
  specialist's instructions carried verbatim as `references/<name>.md`, written
  to `<dest_root>/<name>/skills/<name>/`.
- its **MCP servers** (when the plugin ships a root `.mcp.json`): the file is
  copied to `<dest_root>/<name>/.mcp.json`, where the shared MCP config loader
  (`ava/_mcp_config.py`, scanning `~/.ava/plugins/*/.mcp.json`) merges it in —
  so the bundled servers connect on next use, no extra wiring.
- its **commands** (when the plugin ships `commands/`): the directory is copied
  to `<dest_root>/<name>/commands/`, where `ava/_commands.py:discover_commands`
  (scanning `~/.ava/plugins/*/commands/`) surfaces them as composer `/`-commands —
  no extra wiring.

This is the deterministic format adapter behind `ava plugins install` for
Claude Code plugins. A plugin that bundles none of the above (e.g. hooks-only)
is refused until that piece lands.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Materialized:
    """What a `materialize` call produced, for an honest install message.

    A Claude Code plugin is a container; this records what it actually
    contributed to Ava. `name` is the plugin (folder) name. `shipped_skills`
    lists skills copied verbatim from the plugin's `skills/` (empty when none).
    `skill_name` is the generated orchestrator skill, or None when the plugin
    shipped no `agents/`. `agents` lists the specialist names folded into that
    skill (empty when no skill). `mcp_servers` lists the bundled MCP server
    names carried into the overlay (empty when none). `commands` lists the
    slash-command names copied from the plugin's `commands/` (empty when none).
    """

    name: str
    skill_name: str | None
    agents: list[str]
    mcp_servers: list[str]
    shipped_skills: list[str]
    commands: list[str]


class ClaudeCodePluginError(Exception):
    """A Claude Code plugin could not be read or is of an unsupported shape."""


def is_claude_code_plugin(pkg_dir: Path) -> bool:
    """True if `pkg_dir` is a Claude Code plugin (has a manifest)."""
    return (pkg_dir / ".claude-plugin" / "plugin.json").is_file()


def _read_manifest(pkg_dir: Path) -> tuple[str, str]:
    """Return (name, description) from `.claude-plugin/plugin.json`.

    Raises:
        ClaudeCodePluginError: manifest missing / unparseable / missing the name field.
    """
    manifest = pkg_dir / ".claude-plugin" / "plugin.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ClaudeCodePluginError(f"unreadable plugin.json: {e}") from e
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ClaudeCodePluginError("plugin.json missing a 'name' field")
    description = data.get("description")
    return name, description if isinstance(description, str) else ""


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a markdown file into (frontmatter dict, body).

    Tolerant of marketplace agent files: a leading `---` block of `key: value`
    lines (values may themselves contain colons), then the body. No frontmatter
    returns ({}, text). Unlike the strict skill parser this never raises — an
    agent file's exact frontmatter shape is the upstream author's concern.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    block = text[4:end]
    body = text[end + len("\n---\n") :]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, body


def _reference_text(agent_md: str) -> tuple[str, str, str]:
    """Turn a marketplace agent file into (name, description, reference body).

    Strips the agent's frontmatter and prepends a small header carrying its
    name + description (the upstream "when to invoke" lives there), so the
    materialized reference is self-identifying for both the orchestrator and
    the sub-agent that receives it.
    """
    fields, body = _split_frontmatter(agent_md)
    name = fields.get("name", "")
    description = fields.get("description", "")
    header = f"# {name}\n\n{description}\n\n" if name else ""
    return name, description, header + body.lstrip("\n")


def _first_sentence(text: str) -> str:
    """First sentence of a description, for the orchestrator's dimension list."""
    text = text.strip()
    dot = text.find(". ")
    return text[: dot + 1] if dot != -1 else text


def _orchestrator_skill(
    plugin_name: str, description: str, dimensions: list[tuple[str, str]]
) -> str:
    """Render the generated orchestrator SKILL.md (frontmatter + generic body).

    `dimensions` is [(reference-stem, one-line summary)]; the body is identical
    for any agents-bearing plugin — no per-plugin special-casing.
    """
    desc = description.strip()
    if desc and not desc.endswith("."):
        desc += "."
    front_desc = f"{desc} Triggered on review / review PR / code review / CR.".strip()
    dim_lines = "\n".join(f"- **{stem}** — {summary}" for stem, summary in dimensions)
    return f"""---
name: {plugin_name}
description: {front_desc}
---

# {plugin_name}

You orchestrate this review. You do **not** review code yourself — you dispatch
one autonomous sub-agent per review dimension and aggregate their reports.

## Core principles

- **Sub-agents are autonomous** — each has its own shell, filesystem, and Python
  namespace. Hand it the task and the code location; let it run `git diff`, read
  files, and run tests itself. Don't pour the diff into the prompt.
- **Sub-agents you spawn can only message you** — spawn via `ava.agents.spawn`,
  give them your agent_id so they can report back, then idle and wait.

## Review dimensions

Each dimension's full instructions live in `references/<name>.md`, and each
reference opens with its own when-to-invoke / scope. Available dimensions:

{dim_lines}

## Workflow

1. **Determine scope** — the branch / worktree path to review and the base
   branch to compare against (usually main).
2. **Pick dimensions** — read each reference's own when-to-invoke and enable the
   ones matching the changed files. When unsure, run code-reviewer. Run any
   simplification / polish dimension **last**, only when no critical issues
   remain and the change is non-trivial (> ~50 lines).
3. **Dispatch** — for each enabled dimension: `ava.files.read()` its reference,
   then `ava.agents.spawn()` a sub-agent whose prompt carries the reference body
   + the worktree path + base branch + an instruction to report back via
   `ava.agents.send_message(<your agent_id>, report)`.
   You can dispatch several in parallel.
4. **Aggregate** — once all report, combine into:

   ```markdown
   # Review: [branch] -> [base]

   ## Critical — must fix before merge
   - **[dimension]** file:line — problem + suggestion

   ## Important — should fix
   - **[dimension]** file:line — problem + suggestion

   ## Suggestions
   - **[dimension]** file:line — suggestion

   ## Strengths
   - **[dimension]** what's good

   ## Summary
   [one-line overall assessment]
   ```
"""


def _mcp_server_names(mcp_path: Path) -> list[str]:
    """Validate a Claude Code `.mcp.json` and return its server names, sorted.

    Parses through the same loader the runtime uses, so a malformed bundle
    fails here at install time rather than lazily when an agent first connects.

    Raises:
        ClaudeCodePluginError: the file is not valid JSON or its `mcpServers` is not an object.
    """
    from ava._mcp_config import MCPError, _read_servers

    try:
        return sorted(_read_servers(mcp_path))
    except MCPError as e:
        raise ClaudeCodePluginError(f"bad .mcp.json: {e}") from e


def _shipped_skills(skills_dir: Path) -> list[tuple[Path, str]]:
    """Validate a plugin's `skills/` tree, returning [(skill dir, skill name)].

    A skill is a subdirectory holding a `SKILL.md`; its frontmatter is parsed
    through the same loader the runtime uses, so a malformed bundle fails here
    at install time rather than lazily on the next skill scan. Subdirectories
    without a `SKILL.md` are ignored.

    Raises:
        ClaudeCodePluginError: a `SKILL.md` is unparseable / missing required fields.
    """
    from shared.skill_index import SkillFormatError
    from shared.skill_index import parse_skill_frontmatter as _parse_frontmatter

    found: list[tuple[Path, str]] = []
    for entry in sorted(skills_dir.iterdir()):
        skill_md = entry / "SKILL.md"
        if not (entry.is_dir() and skill_md.is_file()):
            continue
        try:
            fields, _ = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        except SkillFormatError as e:
            raise ClaudeCodePluginError(f"bad skills/{entry.name}/SKILL.md: {e}") from e
        found.append((entry, fields["name"]))
    return found


def materialize(pkg_dir: Path, dest_root: Path) -> Materialized:
    """Materialize a Claude Code plugin at `pkg_dir` into `dest_root/<name>/`.

    Carries whatever the plugin bundles: `skills/` -> copied verbatim to
    `<dest_root>/<name>/skills/`; `agents/` -> a generated orchestrator
    `skills/<name>/SKILL.md` + `references/<agent>.md`; `commands/` -> copied
    verbatim to `<dest_root>/<name>/commands/` (picked up by the composer command
    loader); a root `.mcp.json` -> `<dest_root>/<name>/.mcp.json` (picked up by
    the shared MCP loader). A plugin that bundles none of these is refused.
    Returns what was produced.

    Raises:
        ClaudeCodePluginError: manifest invalid, the plugin bundles nothing
            installable, a bundled `SKILL.md` / `.mcp.json` is malformed, or the
            destination exists.
    """
    name, description = _read_manifest(pkg_dir)
    skills_dir = pkg_dir / "skills"
    shipped = _shipped_skills(skills_dir) if skills_dir.is_dir() else []
    agents_dir = pkg_dir / "agents"
    agent_files = sorted(agents_dir.glob("*.md")) if agents_dir.is_dir() else []
    commands_dir = pkg_dir / "commands"
    command_files = sorted(commands_dir.glob("*.md")) if commands_dir.is_dir() else []
    mcp_src = pkg_dir / ".mcp.json"
    mcp_servers = _mcp_server_names(mcp_src) if mcp_src.is_file() else []
    if not shipped and not agent_files and not command_files and not mcp_servers:
        raise ClaudeCodePluginError(
            f"plugin '{name}' bundles no skills/, agents/, commands/, nor .mcp.json — "
            "nothing to install (hooks-only bundles are not wired yet)."
        )

    dest = dest_root / name
    if dest.exists():
        raise ClaudeCodePluginError(
            f"'{name}' already installed at {dest}; use `ava plugins upgrade {name}`."
        )
    dest.mkdir(parents=True)

    shipped_skills: list[str] = []
    for skill_src, skill_name in shipped:
        shutil.copytree(skill_src, dest / "skills" / skill_src.name)
        shipped_skills.append(skill_name)

    agents: list[str] = []
    if agent_files:
        skill_dir = dest / "skills" / name
        refs_dir = skill_dir / "references"
        refs_dir.mkdir(parents=True)
        dimensions: list[tuple[str, str]] = []
        for agent_md in agent_files:
            stem = agent_md.stem
            a_name, a_desc, ref_text = _reference_text(agent_md.read_text(encoding="utf-8"))
            (refs_dir / f"{stem}.md").write_text(ref_text, encoding="utf-8")
            dimensions.append((a_name or stem, _first_sentence(a_desc) or "review dimension"))
            agents.append(a_name or stem)
        (skill_dir / "SKILL.md").write_text(
            _orchestrator_skill(name, description, dimensions), encoding="utf-8"
        )

    commands: list[str] = []
    if command_files:
        shutil.copytree(commands_dir, dest / "commands")
        commands = [f.stem for f in command_files]

    if mcp_servers:
        shutil.copy(mcp_src, dest / ".mcp.json")

    return Materialized(
        name=name,
        skill_name=name if agent_files else None,
        agents=agents,
        mcp_servers=mcp_servers,
        shipped_skills=shipped_skills,
        commands=commands,
    )
