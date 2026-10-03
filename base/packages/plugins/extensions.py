"""What a plugin contributes, as a value — and the registry of every enabled plugin's.

A plugin does not register anything: its `agent_runtime.py` face exports `contribute()`, a
pure function returning a frozen `PluginContributions`. The loader (`agent.extensions`) calls it
for every enabled plugin and holds the results in an `ExtensionRegistry` instance, which the
composition roots (the agent host, the graph build) hand to the code that consumes them. There
is no process-global registry to mutate at import, so attribution does not ride a ContextVar
and a reload is a new registry.

The surfaces moved here so far are the two the agent runtime reads at window / prompt
establishment: system prompt sections and context notes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from langchain_core.messages import HumanMessage

from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.contributions import Contribution

NoteBuilder = Callable[[AgentSlices], HumanMessage | None]
SectionFn = Callable[[AgentSlices], str]

# Rank a note gets when it states none: after every ranked note, in declaration order.
DEFAULT_NOTE_RANK = 100


@dataclass(frozen=True)
class ContextNote:
    """One standing context note: how to build it, where it sits in the head, and whether a
    fork needs it too.

    `build` returns `None` when the note has nothing to say this time — its layer is disabled,
    its source file is absent, its list is empty. That is the normal way a note opts out.

    `rank` orders the rendered head: lower ranks sit closer to the SystemMessage; equal ranks
    keep declaration order (stable sort). `on_fork` also grafts the note onto a forked agent's
    inherited history; use it only for notes that inherited history renders wrong.
    """

    build: NoteBuilder
    on_fork: bool = False
    rank: int = DEFAULT_NOTE_RANK


@dataclass(frozen=True)
class PluginContributions:
    """Everything one plugin declares for the agent runtime."""

    system_prompt_sections: tuple[SectionFn, ...] = ()
    context_notes: tuple[ContextNote, ...] = ()

    def as_records(self, plugin: str) -> tuple[Contribution, ...]:
        """The same facts as attribution records, spelled the way the manifest declares them
        (`ava plugins inspect` shows these; the manifest check compares them)."""
        sections = (
            Contribution("systemPromptSections", fn.__name__, plugin, fn.__module__)
            for fn in self.system_prompt_sections
        )
        notes = (
            Contribution(
                "contextNotes", n.build.__name__, plugin, f"rank={n.rank} on_fork={n.on_fork}"
            )
            for n in self.context_notes
        )
        return (*sections, *notes)


@dataclass(frozen=True)
class ExtensionRegistry:
    """Every enabled plugin's contributions, in plugin load order. Immutable: a reload builds a
    new one."""

    plugins: tuple[tuple[str, PluginContributions], ...] = ()

    def system_prompt_sections(self) -> Iterator[tuple[str, SectionFn]]:
        """(plugin, section) in load order, then declaration order within a plugin."""
        for plugin, contributions in self.plugins:
            for fn in contributions.system_prompt_sections:
                yield plugin, fn

    def context_notes(self) -> Iterator[tuple[str, ContextNote]]:
        """(plugin, note) in load order, then declaration order within a plugin."""
        for plugin, contributions in self.plugins:
            for note in contributions.context_notes:
                yield plugin, note

    def records(self, plugin: str) -> tuple[Contribution, ...]:
        """One plugin's contributions as attribution records."""
        return tuple(r for name, c in self.plugins if name == plugin for r in c.as_records(plugin))


EMPTY = ExtensionRegistry()
