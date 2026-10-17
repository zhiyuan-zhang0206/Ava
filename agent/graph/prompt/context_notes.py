# meiva:ref agent/graph/prompt/docs/composition.meiva.okf.md#context-order
from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from agent.messages import NoteTag, system_note_message
from base.agents.context import MeivaContext
from base.agents.to_user import read_document
from base.log import logger
from base.packages.plugins.extensions import ContextNote, ExtensionRegistry
from base.packages.skills.tree import identifier, target
from base.paths import workspace_dir

RANK_EXEC_TIMEOUT = 10
RANK_TIMEZONE = 15
RANK_CLUSTER_MEMORY = 20
RANK_AGENT_ID = 30
RANK_PER_AGENT_MEMORY = 40
RANK_INHERITED_MEMORY = 45
RANK_PRELOADED_SKILLS = 50


def _ordered(extensions: ExtensionRegistry, *, fork_only: bool) -> list[ContextNote]:

    plugin_notes = [note for _plugin, note in extensions.context_notes()]
    return [n for n in (*FRAMEWORK_NOTES, *plugin_notes) if n.on_fork or not fork_only]


def context_notes(extensions: ExtensionRegistry, ctx: MeivaContext) -> list[HumanMessage]:

    return _rendered(_ordered(extensions, fork_only=False), ctx)


def fork_notes(extensions: ExtensionRegistry, ctx: MeivaContext) -> list[HumanMessage]:

    return _rendered(_ordered(extensions, fork_only=True), ctx)


def _rendered(entries: list[ContextNote], ctx: MeivaContext) -> list[HumanMessage]:
    built: list[tuple[int, HumanMessage]] = []
    for entry in entries:
        note = entry.build(ctx)
        if note is None:
            continue
        if not isinstance(note, HumanMessage):
            raise TypeError(
                f"context note {entry.build.__name__} returned {type(note).__name__}, "
                "not HumanMessage | None"
            )
        built.append((entry.rank, note))
    return [note for _, note in sorted(built, key=lambda pair: pair[0])]


def _established_agent_id(ctx: MeivaContext, note: str) -> int | None:

    from meiva.sdk_surface.agent_identity import agent_id

    aid = agent_id(ctx)
    if aid is None:
        logger.debug("[context-notes] {} note skipped: no agent identity established", note)
    return aid


_EXEC_TIMEOUT_FRAMING = (
    "Your execute_code call has a hard wall-clock timeout of "
    "{timeout_s:.0f} seconds ({timeout_display}). "
    "If your code exceeds this, it will be killed."
)


def _format_timeout_display(timeout_s: float) -> str:

    minutes = timeout_s / 60
    if minutes == int(minutes):
        n = int(minutes)
        return f"{n} minute" if n == 1 else f"{n} minutes"
    return f"{timeout_s:.0f} seconds"


def exec_timeout_note(ctx: MeivaContext) -> HumanMessage | None:

    if _established_agent_id(ctx, "exec-timeout") is None:
        return None
    timeout_s = ctx.require_agent().read("sandbox", "exec_timeout_seconds")
    return system_note_message(
        content=_EXEC_TIMEOUT_FRAMING.format(
            timeout_s=timeout_s,
            timeout_display=_format_timeout_display(timeout_s),
        ),
        tag=NoteTag.EXEC_TIMEOUT,
        created_at=datetime.now(UTC),
    )


_TIMEZONE_FRAMING = (
    "Conversation timezone: {name} (UTC{offset}). Conversation timestamps "
    "without an offset use this timezone. Tool output, files and source "
    "messages may use other timezones; honor any explicit offset. When you "
    "record a time anywhere outside this conversation (a file, a memory note, "
    "a message to another machine), write ISO-8601 with an offset instead."
)


def _utc_offset(moment: datetime) -> str:

    raw = moment.strftime("%z")
    return f"{raw[:3]}:{raw[3:]}"


def timezone_note(ctx: MeivaContext) -> HumanMessage | None:

    if _established_agent_id(ctx, "timezone") is None:
        return None
    clock = ctx.require_clock()
    now = datetime.now(clock.explicit_zone())
    return system_note_message(
        content=_TIMEZONE_FRAMING.format(name=clock.timezone, offset=_utc_offset(now)),
        tag=NoteTag.TIMEZONE,
        created_at=datetime.now(UTC),
    )


def own_label(ctx: MeivaContext, agent_id: int) -> str | None:

    with ctx.sql.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id=%s", (agent_id,))
        row = cur.fetchone()
    if row is None or not row[0]:
        return None
    return " ".join(str(row[0]).split())


def machine_clause(ctx: MeivaContext) -> str | None:

    from base.cluster.machine import MachineNameMissing

    if ctx.machine is None:
        return None
    try:
        name = ctx.machine.name()
    except MachineNameMissing:
        return None
    return " ".join(name.split()) or None


def _workspace_path(agent_id: int, *, enabled: bool) -> str | None:

    if not enabled:
        return None
    ws = workspace_dir(agent_id)
    try:
        return f"~/{ws.relative_to(Path.home())}"
    except ValueError:
        return str(ws)


def agent_id_note(ctx: MeivaContext) -> HumanMessage | None:

    aid = _established_agent_id(ctx, "agent-id")
    if aid is None:
        return None
    clauses: list[str] = []
    label = own_label(ctx, aid)
    if label:
        clauses.append(f"label: {label}")
    machine = machine_clause(ctx)
    if machine:
        clauses.append(f"machine: {machine}")
    detail = f" ({', '.join(clauses)})" if clauses else ""
    workspace = _workspace_path(
        aid, enabled=ctx.require_agent().read("agent", "workspace_in_system_prompt")
    )
    tail = f" Your workspace is {workspace}." if workspace else ""
    if workspace:
        try:
            document = read_document(workspace_dir(aid))
        except (OSError, ValueError) as exc:
            tail += (
                f"\nThe shared context file could not be read: {exc}. Repair it in your workspace."
            )
        else:
            if document.content:
                tail += (
                    "\n\nCurrent shared context displayed beside your messages:\n"
                    + document.content
                )
    return system_note_message(
        content=f"Your Agent ID is {aid}{detail}.{tail}",
        tag=NoteTag.AGENT_ID,
        created_at=datetime.now(UTC),
    )


_PRELOADED_SKILLS_FRAMING = (
    "Preloaded skills — each section below contains a skill's complete SKILL.md. "
    "Follow these instructions as standing guidance; you do not need to reload "
    "these bodies. Read any supporting files referenced by a skill with "
    "meiva.help(meiva.skills.<path>) or meiva.files.read."
)


def preloaded_skills_note(ctx: MeivaContext) -> HumanMessage | None:

    from agent.graph.prompt.capabilities import resolve_prompt_skills

    slices = ctx.require_agent()
    skills = resolve_prompt_skills(
        slices.prompt.skills_to_expand_at_start,
        slices.prompt.sdk_disable,
        config_field="skills_to_expand_at_start",
    )
    if not skills:
        return None
    rendered = _render_skill_bodies(skills, label="preloaded-skills")
    if rendered is None:
        return None
    sections, _injected = rendered
    content = _PRELOADED_SKILLS_FRAMING + "\n\n" + "\n\n---\n\n".join(sections)
    return system_note_message(
        content=content, tag=NoteTag.PRELOADED_SKILLS, created_at=datetime.now(UTC)
    )


_FORK_TAIL_SKILLS_FRAMING = (
    "Additional skills for you — each section below contains a skill's complete "
    "SKILL.md. Follow these instructions as standing guidance alongside the "
    "skills already in your context; you do not need to reload these bodies."
)


def _render_skill_bodies(skills: list[Any], *, label: str) -> tuple[list[str], list[str]] | None:

    sections: list[str] = []
    injected: list[str] = []
    for skill in skills:
        skill_md = Path(skill["path"]) / "SKILL.md"
        try:
            body = skill_md.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning(
                "[{}] skill {} SKILL.md unreadable ({}), skipping",
                label,
                skill["name"],
                exc,
            )
            continue
        sections.append(f"## meiva.skills.{target(skill)}\n\n{body}")
        injected.append(identifier(skill))
    if not sections:
        return None
    logger.info("[{}] injecting {} skill(s): {}", label, len(injected), ", ".join(injected))
    return sections, injected


def fork_tail_skills_note(names: list[str], sdk_disable: Sequence[str]) -> HumanMessage | None:

    if not names:
        return None
    from agent.graph.prompt.capabilities import resolve_prompt_skills

    skills = resolve_prompt_skills(names, sdk_disable, config_field="fork_tail_skills")
    rendered = _render_skill_bodies(skills, label="fork-tail-skills")
    if rendered is None:
        return None
    sections, _injected = rendered
    content = _FORK_TAIL_SKILLS_FRAMING + "\n\n" + "\n\n---\n\n".join(sections)
    return system_note_message(
        content=content, tag=NoteTag.PRELOADED_SKILLS, created_at=datetime.now(UTC)
    )


FRAMEWORK_NOTES: tuple[ContextNote, ...] = (
    ContextNote(build=exec_timeout_note, rank=RANK_EXEC_TIMEOUT),
    ContextNote(build=timezone_note, rank=RANK_TIMEZONE),
    ContextNote(build=agent_id_note, on_fork=True, rank=RANK_AGENT_ID),
    ContextNote(build=preloaded_skills_note, on_fork=True, rank=RANK_PRELOADED_SKILLS),
)
