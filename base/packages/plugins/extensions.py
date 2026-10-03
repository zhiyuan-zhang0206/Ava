"""What a plugin contributes, as a value — and the registry of every enabled plugin's.

A plugin does not register anything: its `agent_runtime.py` face exports `contribute()`, a
pure function returning a frozen `PluginContributions`. The loader (`agent.extensions`) calls it
for every enabled plugin and holds the results in an `ExtensionRegistry` instance, which the
composition roots (the agent host, the graph build) hand to the code that consumes them. There
is no process-global registry to mutate at import, so attribution does not ride a ContextVar
and a reload is a new registry.

The surfaces moved here so far: system prompt sections and context notes (read at window /
prompt establishment), graph-edge hooks and plugin state classes (read once, when the graph is
built). Each is a value in the registry the graph build and the host are handed, so one registry
is one coherent plugin set for both.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from base.host.env.agent_slices import AgentSlices
from base.packages.plugins.contributions import Contribution
from base.packages.plugins.inspector import InspectWidgetSpec
from base.telemetry.metrics.plugin_metrics import MetricSpec

NoteBuilder = Callable[[AgentSlices], HumanMessage | None]
SectionFn = Callable[[AgentSlices], str]

HookPoint = Literal["after_init", "before_llm", "before_exec", "after_exec"]
HOOK_POINTS: tuple[HookPoint, ...] = ("after_init", "before_llm", "before_exec", "after_exec")


class GraphHook(Protocol):
    """What the hook runner calls: `agent.hooks.Hook` instances satisfy it structurally.

    The agent layer owns the typed base class; this protocol keeps the declaration in `base`
    free of the agent state types.
    """

    @property
    def name(self) -> str: ...

    async def __call__(self, state: Any, runtime: Any, config: Any, /) -> dict[str, Any] | None: ...


# The one framework core channel a plugin may declare and write (its add_messages reducer defines
# the merge contract); every other core key is framework-managed each turn. The plugin's field of
# that name shares the base channel instead of getting a `<plugin>__` prefix.
PLUGIN_WRITABLE_BASE_FIELDS: frozenset[str] = frozenset({"messages"})

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
    after_init: tuple[GraphHook, ...] = ()
    before_llm: tuple[GraphHook, ...] = ()
    before_exec: tuple[GraphHook, ...] = ()
    after_exec: tuple[GraphHook, ...] = ()
    # Pydantic models whose fields become LangGraph channels `<plugin>__<field>`; a plugin reads
    # and writes them through its own `PluginStateHandle(cls, plugin)`.
    state: tuple[type[BaseModel], ...] = ()
    # Data surfaces read by processes that load no agent runtime (the gateway, the Grafana supply):
    # a plugin's `metrics.py` / `inspector.py` face declares these, and `base.packages.plugins.data_registry`
    # validates them and fills each spec's `plugin`.
    metrics: tuple[MetricSpec, ...] = ()
    inspect_widgets: tuple[InspectWidgetSpec, ...] = ()

    def hooks(self, point: HookPoint) -> tuple[GraphHook, ...]:
        return getattr(self, point)

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
        hooks = (
            Contribution("hooks", point, plugin, f"{type(h).__module__}.{type(h).__qualname__}")
            for point in HOOK_POINTS
            for h in self.hooks(point)
        )
        state = (
            Contribution(
                "state",
                name if name in PLUGIN_WRITABLE_BASE_FIELDS else f"{plugin}__{name}",
                plugin,
                f"{cls.__name__}.{name}",
            )
            for cls in self.state
            for name in cls.model_fields
        )
        metrics = (Contribution("metrics", spec.name, plugin, spec.title) for spec in self.metrics)
        widgets = (
            Contribution("inspectWidgets", spec.id, plugin, spec.kind)
            for spec in self.inspect_widgets
        )
        return (*sections, *notes, *hooks, *state, *metrics, *widgets)


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

    def hooks(self, point: HookPoint) -> Iterator[tuple[str, GraphHook]]:
        """(plugin, hook) at one graph edge, in load order, then declaration order."""
        for plugin, contributions in self.plugins:
            for hook in contributions.hooks(point):
                yield plugin, hook

    def state_classes(self) -> Iterator[tuple[str, type[BaseModel]]]:
        """(plugin, state class) in load order, then declaration order."""
        for plugin, contributions in self.plugins:
            for cls in contributions.state:
                yield plugin, cls

    def metrics(self) -> Iterator[MetricSpec]:
        """Every plugin's metric specs, in load order, then declaration order."""
        for _plugin, contributions in self.plugins:
            yield from contributions.metrics

    def inspect_widgets(self) -> Iterator[InspectWidgetSpec]:
        """Every plugin's inspector widget specs, in load order, then declaration order."""
        for _plugin, contributions in self.plugins:
            yield from contributions.inspect_widgets

    def records(self, plugin: str) -> tuple[Contribution, ...]:
        """One plugin's contributions as attribution records."""
        return tuple(r for name, c in self.plugins if name == plugin for r in c.as_records(plugin))


EMPTY = ExtensionRegistry()
