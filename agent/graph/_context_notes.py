"""The ordered registry of standing context notes + the framework's own.

A context note is a short system-styled message that sits behind the
SystemMessage for the life of a context window. `init_context` lays the whole
set down at the two moments a window is established — an agent's first wake, and
the turn after any compaction — so a note is written once here rather than at
each of those call sites.

Registration mirrors `_SYSTEM_PROMPT_SECTIONS`: framework notes register at
import of this module, plugins append theirs when `_load_extensions` imports
them, and `clear_plugin_registrations` truncates back to
`_FRAMEWORK_NOTE_COUNT` so a plugin reload drops only the plugin tail.

Rendering order is by `rank`, not registration order: the reading order the
head is supposed to have — exec timeout, then the shared memory index, then the
agent id, then the per-agent memory index, then preloaded skills — spans the
framework/plugin boundary, so "framework notes first, then plugin notes in load
order" cannot express it. Lower ranks sit closer to the SystemMessage; equal
ranks keep registration order (the sort is stable). Notes registered without an
explicit rank default to `DEFAULT_RANK` and land after every ranked note, still
in registration order among themselves.

`on_fork` marks the notes a forked agent also needs. A fork inherits the source
agent's whole conversation, so its window is never established from empty and
`init_context` never runs for it; what it needs instead is the subset of notes
the inherited history gets *wrong* — see `fork_notes`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage

from agent.messages import NoteTag, system_note_message
from shared import plugin_contributions
from shared.config import settings
from shared.config.turn_view import turn_settings
from shared.log import logger

NoteBuilder = Callable[[], HumanMessage | None]

# The rank scale for the standing head — the reading order, lowest first:
# the operational constants (exec timeout, then the cluster clock), then the
# shared world (cluster memory index), then identity (agent id), then the
# personal store (per-agent memory index), then preloaded skill bodies. The
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
RANK_PRELOADED_SKILLS = 50
DEFAULT_RANK = 100


@dataclass(frozen=True)
class ContextNote:
    """One registered note: how to build it, where it sits in the head, and
    whether a fork needs it too.

    `build` returns `None` when the note has nothing to say this time — its
    layer is disabled, its source file is absent, its list is empty. That is the
    normal way a note opts out; the registry never asks why.

    `rank` orders the rendered head: lower ranks sit closer to the
    SystemMessage; equal ranks keep registration order (stable sort).
    """

    build: NoteBuilder
    on_fork: bool
    rank: int


_CONTEXT_NOTES: list[ContextNote] = []


def register_context_note(
    *, on_fork: bool = False, rank: int = DEFAULT_RANK
) -> Callable[[NoteBuilder], NoteBuilder]:
    """Register a standing context note — laid down by `init_context` whenever a
    context window is established.

    `on_fork=True` also grafts it onto a forked agent's inherited history; use it
    only for notes that inherited history renders wrong (a stale snapshot, an
    identity naming the source agent), not for everything a fresh agent would get.

    `rank` sets where the note lands in the head — lower is closer to the
    SystemMessage, ties keep registration order. Framework and ava_memory notes
    pin explicit ranks (see the scale above); a plugin that does not care leaves
    the default and renders after every ranked note.
    """

    def decorate(fn: NoteBuilder) -> NoteBuilder:
        _CONTEXT_NOTES.append(ContextNote(build=fn, on_fork=on_fork, rank=rank))
        plugin_contributions.record(
            "contextNotes", fn.__name__, detail=f"rank={rank} on_fork={on_fork}"
        )
        return fn

    return decorate


def context_notes() -> list[HumanMessage]:
    """Every registered note in rank order (ties: registration order), skipping
    the ones with nothing to say.

    Built fresh at each call: notes that read state off disk (the memory
    indexes) then pick up whatever was written during the window just compacted
    away.
    """
    built = [(entry.rank, note) for entry in _CONTEXT_NOTES if (note := entry.build()) is not None]
    return [note for _, note in sorted(built, key=lambda pair: pair[0])]


def fork_notes() -> list[HumanMessage]:
    """The `on_fork` subset, in the same rank order as `context_notes` — what a
    freshly forked agent needs grafted onto the history it inherited from the
    agent it was forked from."""
    built = [
        (entry.rank, note)
        for entry in _CONTEXT_NOTES
        if entry.on_fork and (note := entry.build()) is not None
    ]
    return [note for _, note in sorted(built, key=lambda pair: pair[0])]


# ── Framework-owned notes ──


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


@register_context_note(rank=RANK_EXEC_TIMEOUT)
def exec_timeout_note() -> HumanMessage | None:
    """A context note stating the execute_code hard timeout.

    Returns ``None`` when this process has no established agent identity."""
    from ava._boot import _agent_id as _raw_aid

    if _raw_aid is None:
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


@register_context_note(rank=RANK_TIMEZONE)
def timezone_note() -> HumanMessage | None:
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
    from ava._boot import _agent_id as _raw_aid

    if _raw_aid is None:
        return None
    now = datetime.now(ZoneInfo(settings.general.timezone))
    return system_note_message(
        content=_TIMEZONE_FRAMING.format(name=settings.general.timezone, offset=_utc_offset(now)),
        tag=NoteTag.TIMEZONE,
        created_at=datetime.now(UTC),
    )


@register_context_note(on_fork=True, rank=RANK_AGENT_ID)
def agent_id_note() -> HumanMessage | None:
    """A context note stating the agent's own id.

    It lives outside the SystemMessage (as a system-styled HumanMessage) so a
    fork — which copies the source agent's full conversation including the
    SystemMessage — does not carry a stale id into the new agent. That is also
    why it is an `on_fork` note: the inherited SystemMessage names the source.

    Returns ``None`` when this process has no established agent identity."""
    from ava._boot import _agent_id as _raw_aid

    aid = _raw_aid
    if aid is None:
        return None
    return system_note_message(
        content=f"Your Agent ID is {aid}.",
        tag=NoteTag.AGENT_ID,
        created_at=datetime.now(UTC),
    )


# Agent-visible framing for the preloaded-skills note. Same carrier and lifetime
# as the other context notes, so the framing mirrors them: standing guidance the
# agent has already read, not something to go open.
_PRELOADED_SKILLS_FRAMING = (
    "Preloaded skills — the full text of the skills your configuration "
    "(skills_to_expand_at_start) loads at the start of every session and "
    "re-injects after each compact. Their instructions are active from your "
    "first turn: treat them as standing guidance you have already read, not as "
    "something to go open. Each section below is one skill's complete SKILL.md; "
    "its other files live beside it — reach them with ava.help(ava.skills.<path>) "
    "or ava.files.read."
)


@register_context_note(on_fork=True, rank=RANK_PRELOADED_SKILLS)
def preloaded_skills_note() -> HumanMessage | None:
    """The full SKILL.md body of every skill named in
    `turn_settings.agent.skills_to_expand_at_start`, concatenated into one note.

    Resolution (wildcard, identifier-then-name, warn-and-skip) is shared with
    the capabilities index via `resolve_prompt_skills`. Returns ``None`` — no
    empty note — when the list is empty, `skills` is SDK-disabled, or nothing
    resolves. Skips (with a warning) any resolved skill whose SKILL.md has since
    become unreadable rather than aborting the whole note.

    `on_fork`: the inherited history carries the SOURCE agent's preloaded
    skills (its own `skills_to_expand_at_start`); the fork grafts the new
    agent's own set after `_handle_fork` strips the inherited note — exactly
    one copy, owned by the agent reading it (issue #1320)."""
    from agent.graph._capabilities import resolve_prompt_skills

    skills = resolve_prompt_skills(
        turn_settings.agent.skills_to_expand_at_start,
        config_field="skills_to_expand_at_start",
    )
    if not skills:
        return None

    import ava

    sections: list[str] = []
    injected: list[str] = []
    for skill in skills:
        skill_md = Path(skill["path"]) / "SKILL.md"
        try:
            body = skill_md.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning(
                "[preloaded-skills] skill {} SKILL.md unreadable ({}), skipping",
                skill["name"],
                exc,
            )
            continue
        sections.append(f"## ava.skills.{ava.skills.identifier(skill)}\n\n{body}")
        injected.append(ava.skills.identifier(skill))

    if not sections:
        return None
    logger.info("[preloaded-skills] injecting {} skill(s): {}", len(injected), ", ".join(injected))
    content = _PRELOADED_SKILLS_FRAMING + "\n\n" + "\n\n---\n\n".join(sections)
    return system_note_message(
        content=content, tag=NoteTag.PRELOADED_SKILLS, created_at=datetime.now(UTC)
    )


# Count of framework-owned notes, snapshotted after the registrations above (all
# at module import). Everything appended later comes from a plugin via
# `_load_extensions`; `clear_plugin_registrations` truncates back to this count
# so a plugin reload drops only the plugin tail — the framework notes are never
# re-registered in-process, so clearing them would lose them for the rest of the run.
_FRAMEWORK_NOTE_COUNT = len(_CONTEXT_NOTES)
