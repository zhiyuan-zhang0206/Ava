"""The ordered registry of standing context notes + the framework's own.

A context note is a short system-styled message that sits behind the
SystemMessage for the life of a context window. `init_context` lays the whole
set down at the two moments a window is established — an agent's first wake, and
the turn after any compaction — so a note is written once here rather than at
each of those call sites.

The framework's own notes are the constant `FRAMEWORK_NOTES` below; a plugin declares its notes
in `contribute()` (`base.packages.plugins.extensions`), and the notes it contributes arrive here
through the `ExtensionRegistry` the caller holds — nothing registers at import.

Rendering order is by `rank`, not declaration order: the reading order the
head is supposed to have — exec timeout, then the shared memory index, then the
agent id, then the per-agent memory index, then preloaded skills — spans the
framework/plugin boundary, so "framework notes first, then plugin notes in load
order" cannot express it. Lower ranks sit closer to the SystemMessage; equal
ranks keep declaration order (the sort is stable). Notes declared without an
explicit rank default to `DEFAULT_NOTE_RANK` and land after every ranked note, still
in declaration order among themselves.

`on_fork` marks the notes a forked agent also needs. A fork inherits the source
agent's whole conversation, so its window is never established from empty and
`init_context` never runs for it; what it needs instead is the subset of notes
the inherited history gets *wrong* — see `fork_notes`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from agent.messages import NoteTag, system_note_message
from base.clock import Clock
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.log import logger
from base.packages.plugins.extensions import ContextNote, ExtensionRegistry
from base.paths import workspace_dir

# The rank scale for the standing head — the reading order, lowest first:
# the operational constants (exec timeout, then the cluster clock), then the
# shared world (cluster memory index), then identity (agent id with label +
# machine + workspace), then the
# personal store (per-agent memory index, then the inheritable blocks read
# from the chain above), then preloaded skill bodies. The
# ava_memory plugin pins the two memory ranks; everything else registered
# without a rank defaults to `DEFAULT_RANK` and renders after all ranked notes.
#
# Ranks 10-19 are the stable band, and that is a prompt-cache decision, not a
# reading-order one. Both providers cache on a byte prefix, so what a note costs
# depends on what sits in front of it: everything from rank 20 on is behind the
# cluster memory index, which is re-read off disk at every window establishment
# and changes whenever any agent writes memory. A note placed after it re-caches
# on someone else's memory write. The 10-19 band is cluster-identical (same
# bytes for every agent on the API key, so they share one cached prefix) and
# changes only on a config edit that already forces an agent restart.
RANK_EXEC_TIMEOUT = 10
RANK_TIMEZONE = 15
RANK_CLUSTER_MEMORY = 20
RANK_AGENT_ID = 30
RANK_PER_AGENT_MEMORY = 40
RANK_INHERITED_MEMORY = 45
RANK_PRELOADED_SKILLS = 50


def _ordered(extensions: ExtensionRegistry, *, fork_only: bool) -> list[ContextNote]:
    """The framework's notes then every plugin's, in declaration order."""
    plugin_notes = [note for _plugin, note in extensions.context_notes()]
    return [n for n in (*FRAMEWORK_NOTES, *plugin_notes) if n.on_fork or not fork_only]


def context_notes(extensions: ExtensionRegistry, slices: AgentSlices) -> list[HumanMessage]:
    """Every note in rank order (ties: declaration order), skipping
    the ones with nothing to say.

    Built fresh at each call: notes that read state off disk (the memory
    indexes) then pick up whatever was written during the window just compacted
    away.
    """
    return _rendered(_ordered(extensions, fork_only=False), slices)


def fork_notes(extensions: ExtensionRegistry, slices: AgentSlices) -> list[HumanMessage]:
    """The `on_fork` subset, in the same rank order as `context_notes` — what a
    freshly forked agent needs grafted onto the history it inherited from the
    agent it was forked from."""
    return _rendered(_ordered(extensions, fork_only=True), slices)


def _rendered(entries: list[ContextNote], slices: AgentSlices) -> list[HumanMessage]:
    built: list[tuple[int, HumanMessage]] = []
    for entry in entries:
        note = entry.build(slices)
        if note is None:
            continue
        if not isinstance(note, HumanMessage):
            raise TypeError(
                f"context note {entry.build.__name__} returned {type(note).__name__}, "
                "not HumanMessage | None"
            )
        built.append((entry.rank, note))
    return [note for _, note in sorted(built, key=lambda pair: pair[0])]


# ── Framework-owned notes ──


def _established_agent_id(note: str) -> int | None:
    """Resolve the current agent identity for a standing note, or None.

    Reads through `ava.agent_identity.agent_id()`, which resolves the hosted runner's
    turn contextvar first: the agent host hosts many agents' turns in one
    process and establishes no process-wide id, so reading the `_agent_id`
    process slot directly would silently drop the note from every hosted head
    (task #3939 — the identity line was missing for two weeks before the skip
    was noticed). The skip is debug-logged: a legitimately absent identity
    (snapshot renders, dev REPL, container mode) stays quiet at normal levels,
    but a production regression leaves a trace.
    """
    from ava.agent_identity import agent_id

    aid = agent_id()
    if aid is None:  # pyright: ignore[reportUnnecessaryComparison] — agent_id() is None pre-bootstrap.
        logger.debug("[context-notes] {} note skipped: no agent identity established", note)
    return aid


_EXEC_TIMEOUT_FRAMING = (
    "Your execute_code call has a hard wall-clock timeout of "
    "{timeout_s:.0f} seconds ({timeout_display}). "
    "If your code exceeds this, it will be killed."
)


def _format_timeout_display(timeout_s: float) -> str:
    """Format the timeout for display, e.g. '5 minutes' or '300 seconds'."""
    minutes = timeout_s / 60
    if minutes == int(minutes):
        n = int(minutes)
        return f"{n} minute" if n == 1 else f"{n} minutes"
    return f"{timeout_s:.0f} seconds"


def exec_timeout_note(_slices: AgentSlices) -> HumanMessage | None:
    """A context note stating the execute_code hard timeout.

    Returns ``None`` when this process has no established agent identity."""
    if _established_agent_id("exec-timeout") is None:
        return None
    timeout_s = settings.sandbox.exec_timeout_seconds
    return system_note_message(
        content=_EXEC_TIMEOUT_FRAMING.format(
            timeout_s=timeout_s,
            timeout_display=_format_timeout_display(timeout_s),
        ),
        tag=NoteTag.EXEC_TIMEOUT,
        created_at=datetime.now(UTC),
    )


_TIMEZONE_FRAMING = (
    "Current timezone: {name} (UTC{offset}). All timestamps you see are in "
    "this timezone — they carry no timezone suffix of their own. When you "
    "record a time anywhere outside this conversation (a file, a memory note, "
    "a message to another machine), write ISO-8601 with an offset instead."
)


def _utc_offset(moment: datetime) -> str:
    """`moment`'s UTC offset as ``+08:00`` / ``-07:00`` (strftime gives ``+0800``)."""
    raw = moment.strftime("%z")
    return f"{raw[:3]}:{raw[3:]}"


def timezone_note(_slices: AgentSlices) -> HumanMessage | None:
    """A context note declaring the cluster's timezone once, so the timestamps
    themselves don't have to carry it.

    `settings.general.timezone` is cluster-pinned, so the timezone is a
    constant across every timestamp an agent will ever see — repeating it on
    each one bought nothing and cost ambiguity (`%Z` renders `PDT` in June and
    `PST` in December for one unchanged setting, and `CST` names two different
    zones). Declared here, the IANA name is unambiguous and the numeric offset
    is spelled out beside it.

    Not an `on_fork` note: a fork inherits its source agent's history, and both
    agents are in the same cluster, so the inherited declaration is correct.

    The offset is resolved when the window is established. Across a DST
    boundary within one long-lived window the numeric half can go stale; the
    IANA name beside it stays authoritative, and a `AVA_TIMEZONE` edit is
    `restart_required: agent`, which re-establishes the head.

    Returns ``None`` when this process has no established agent identity."""
    if _established_agent_id("timezone") is None:
        return None
    clock = Clock.from_settings()
    now = datetime.now(clock.explicit_zone())
    return system_note_message(
        content=_TIMEZONE_FRAMING.format(name=clock.timezone, offset=_utc_offset(now)),
        tag=NoteTag.TIMEZONE,
        created_at=datetime.now(UTC),
    )


def _own_label(agent_id: int) -> str | None:
    """The agent's current label, whitespace-normalized, or None.

    Fail-soft on purpose: the identity line must render even when the label
    read cannot (DB blip, row not yet auto-named), so every failure degrades to
    "no label clause" — never to a missing identity line. A label is free text
    (set by the agent via `ava.self.set_label` or by the gateway), so its
    whitespace is collapsed before it enters the one-line note.
    """
    import ava

    try:
        with ava.DB.cursor() as cur:
            cur.execute("SELECT label FROM agents WHERE id=%s", (agent_id,))
            row = cur.fetchone()
    except Exception:  # fail-soft by design: the identity line outranks the label clause
        logger.opt(exception=True).warning(
            "[context-notes] agent-id label read failed; the identity line renders without a label"
        )
        return None
    if row is None or not row[0]:
        return None
    return " ".join(str(row[0]).split())


def _machine_clause() -> str | None:
    """The host's machine name, whitespace-normalized, or None when unset.

    Fail-soft like the label: a host whose machine name cannot be resolved
    still states the agent's identity line."""
    from base.cluster.machine import MachineNameMissing, machine_name

    try:
        name = machine_name()
    except MachineNameMissing:  # an unset machine name only drops the machine clause
        return None
    return " ".join(name.split()) or None


def _workspace_path(agent_id: int) -> str | None:
    """The agent's concrete workspace path, or None when the section is off.

    This is where the concrete path lives: the `# Workspace` prompt section is
    deliberately id-free (a fork copies the SystemMessage verbatim, so a
    baked-in path would name the source agent's folder), so the per-agent path
    rides this note instead — regrafted by a fork. Same on/off gate as the
    section: bench runners that turn the section off keep their prompts free of
    workspace chatter."""
    if not settings.agent.workspace_in_system_prompt:
        return None
    ws = workspace_dir(agent_id)
    try:
        return f"~/{ws.relative_to(Path.home())}"
    except ValueError:
        return str(ws)


def agent_id_note(_slices: AgentSlices) -> HumanMessage | None:
    """A context note stating the agent's own identity: id, label, machine,
    workspace path — each clause fail-soft.

    It lives outside the SystemMessage (as a system-styled HumanMessage) so a
    fork — which copies the source agent's full conversation including the
    SystemMessage — does not carry a stale identity into the new agent. That is
    also why it is an `on_fork` note: the inherited SystemMessage names the
    source. The note is also where the `# Workspace` section points for the
    concrete path, for the same fork-safety reason.

    Returns ``None`` when this process has no established agent identity."""
    aid = _established_agent_id("agent-id")
    if aid is None:
        return None
    clauses: list[str] = []
    label = _own_label(aid)
    if label:
        clauses.append(f"label: {label}")
    machine = _machine_clause()
    if machine:
        clauses.append(f"machine: {machine}")
    detail = f" ({', '.join(clauses)})" if clauses else ""
    workspace = _workspace_path(aid)
    tail = f" Your workspace is {workspace}." if workspace else ""
    return system_note_message(
        content=f"Your Agent ID is {aid}{detail}.{tail}",
        tag=NoteTag.AGENT_ID,
        created_at=datetime.now(UTC),
    )


# Agent-visible framing for the preloaded-skills note. Same carrier and lifetime
# as the other context notes, so the framing mirrors them: standing guidance the
# agent has already read, not something to go open.
_PRELOADED_SKILLS_FRAMING = (
    "Preloaded skills — each section below contains a skill's complete SKILL.md. "
    "Follow these instructions as standing guidance; you do not need to reload "
    "these bodies. Read any supporting files referenced by a skill with "
    "ava.help(ava.skills.<path>) or ava.files.read."
)


def preloaded_skills_note(slices: AgentSlices) -> HumanMessage | None:
    """The full SKILL.md body of every skill named in
    `Prompt.skills_to_expand_at_start`, concatenated into one note.

    Resolution (wildcard, identifier-then-name, warn-and-skip) is shared with
    the capabilities index via `resolve_prompt_skills`. Returns ``None`` — no
    empty note — when the list is empty, `skills` is SDK-disabled, or nothing
    resolves. Skips (with a warning) any resolved skill whose SKILL.md has since
    become unreadable rather than aborting the whole note.

    `on_fork`: the inherited history carries the SOURCE agent's preloaded
    skills (its own `skills_to_expand_at_start`); the fork grafts the new
    agent's own set after `_handle_fork` strips the inherited note — exactly
    one copy, owned by the agent reading it (issue #1320)."""
    from agent.graph.prompt.capabilities import resolve_prompt_skills

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


# Agent-visible framing for the fork tail-graft note — skills a fork's config
# added that the inherited context does not carry. Appended at the very TAIL of
# the rebuilt head (after the fork marker and the on_fork notes), so nothing in
# front of it changes: the inherited prefix stays byte-identical for the
# provider's prefix cache (docs/decisions/2026-09-10-preset-in-config-overlay-fork-cache).
_FORK_TAIL_SKILLS_FRAMING = (
    "Additional skills for you — each section below contains a skill's complete "
    "SKILL.md. Follow these instructions as standing guidance alongside the "
    "skills already in your context; you do not need to reload these bodies."
)


def _render_skill_bodies(skills: list[Any], *, label: str) -> tuple[list[str], list[str]] | None:
    """Read each resolved skill's SKILL.md into a `## ava.skills.<path>` section.

    Returns ``(sections, injected_identifiers)``, or None when every skill is
    unreadable. Unreadable bodies warn + skip (same posture as the whole-note
    builder) rather than aborting the note."""
    import ava

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
        sections.append(f"## ava.skills.{ava.skills.identifier(skill)}\n\n{body}")
        injected.append(ava.skills.identifier(skill))
    if not sections:
        return None
    logger.info("[{}] injecting {} skill(s): {}", label, len(injected), ", ".join(injected))
    return sections, injected


def fork_tail_skills_note(names: list[str], sdk_disable: Sequence[str]) -> HumanMessage | None:
    """The full SKILL.md bodies of the fork's skill ADDITIONS, for grafting at
    the context tail.

    `names` is the delta the gateway computed at spawn —
    `(fork_inject - source_inject) - fork_expand` — carried in the fork
    inbound's payload. Resolution (identifier-then-name, warn-and-skip) is
    shared with the capabilities index and the preloaded-skills note. Tagged
    `preloaded_skills` so the NEXT fork strips it with the other skill notes
    and grafts its own. Returns None when the list is empty or nothing resolves.
    """
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


# The framework-owned standing notes. Plugins declare theirs in `contribute()`; both kinds meet in
# `context_notes` / `fork_notes`, which order them by rank.
FRAMEWORK_NOTES: tuple[ContextNote, ...] = (
    ContextNote(build=exec_timeout_note, rank=RANK_EXEC_TIMEOUT),
    ContextNote(build=timezone_note, rank=RANK_TIMEZONE),
    ContextNote(build=agent_id_note, on_fork=True, rank=RANK_AGENT_ID),
    ContextNote(build=preloaded_skills_note, on_fork=True, rank=RANK_PRELOADED_SKILLS),
)
