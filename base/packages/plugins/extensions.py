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
from dataclasses import dataclass, fields
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from base.agents.context import AvaContext
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.provider_contract import ProviderContribution
from base.packages.plugins.contributions import Contribution
from base.packages.plugins.inspector import InspectWidgetSpec
from base.telemetry.metrics.plugin_metrics import MetricSpec

# A note builder returns a langchain `HumanMessage`, or None when it has nothing to say; the type is
# `object` here because this module is imported by processes that must stay off the LM stack (a
# child's surface load), and `agent.graph.prompt.context_notes` checks what a builder returns.
NoteBuilder = Callable[[AvaContext], object | None]


class SectionFn(Protocol):
    """A plugin prompt section reads the caller's explicit model catalog."""

    @property
    def __name__(self) -> str: ...

    def __call__(self, slices: AgentSlices, /, *, catalog: ModelCatalog) -> str: ...


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

# The core channels the SDK itself writes through `ava.state_update` with no plugin declaring them
# (`ava.security` appends prompt-injection findings; `ava.self.attach` appends the files to hand
# the model next); every other undeclared core key is a typo.
SDK_WRITTEN_BASE_FIELDS: frozenset[str] = frozenset({"security_findings", "attach"})

# Rank a note gets when it states none: after every ranked note, in declaration order.
DEFAULT_NOTE_RANK = 100


@dataclass(frozen=True)
class ContextNote:
    """One standing context note: how to build it, where it sits in the head, and whether a
    fork needs it too.

    `build` receives the explicit invocation context and returns `None` when the note has nothing to say this time — its layer is disabled,
    its source file is absent, its list is empty. That is the normal way a note opts out.

    `rank` orders the rendered head: lower ranks sit closer to the SystemMessage; equal ranks
    keep declaration order (stable sort). `on_fork` also grafts the note onto a forked agent's
    inherited history; use it only for notes that inherited history renders wrong.
    """

    build: NoteBuilder
    on_fork: bool = False
    rank: int = DEFAULT_NOTE_RANK


@dataclass(frozen=True)
class SdkNamespace:
    """A top-level `ava.<name>` namespace a plugin adds; `expand` also promotes it into the system
    prompt's expanded SDK reference, ahead of the framework's list."""

    name: str
    module: ModuleType | SimpleNamespace
    expand: bool = False


@dataclass(frozen=True)
class SdkMember:
    """A callable a plugin hangs on an existing namespace (`ava.<namespace>.<name>(...)`)."""

    namespace: str
    name: str
    fn: Callable[..., Any]


@dataclass(frozen=True)
class SdkWrap:
    """A layer around the `ava` callable at dotted `target` (`wrapper(inner, *args, **kwargs)`).

    Layers stack in plugin load order, then declaration order; later layers wrap outermost."""

    target: str
    wrapper: Callable[..., Any]


@dataclass(frozen=True)
class PluginContributions:
    """Everything one plugin declares, field by field, whichever face it comes from.

    A plugin's `plugin.py` (the SDK surface, loaded by every process that runs agent code) declares
    `sdk_*`, `skill_sources`, `config` and `flags`; its `agent_runtime.py` declares the agent
    runtime's fields; `metrics.py` / `inspector.py` the data surfaces. A face fills only its own
    fields, so the faces of one plugin merge with `merged`.
    """

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
    # Model providers, declared by a plugin's `provider.py`: loaded by every process that builds or
    # validates a chat model, installed into the process's model catalog by
    # `base/lm/plugin_providers.py` (the one writer).
    providers: tuple[ProviderContribution, ...] = ()
    # The SDK surface (`ava.*`): namespaces (optionally promoted into the prompt's expanded SDK
    # reference), members hung on an existing namespace, wrap layers, and skill-root providers.
    sdk_namespaces: tuple[SdkNamespace, ...] = ()
    sdk_members: tuple[SdkMember, ...] = ()
    sdk_wraps: tuple[SdkWrap, ...] = ()
    skill_sources: tuple[Callable[[], list[Path]], ...] = ()
    # Dotted `ava` paths promoted into the expanded SDK reference without declaring a namespace
    # (a member group, a framework sub-namespace the plugin extends).
    sdk_expansions: tuple[str, ...] = ()
    # One frozen BaseModel bound once from `$AVA_HOME/configs/<plugin>/config.json`.
    config: type[BaseModel] | None = None
    # Fully qualified `<domain>.<field>` Core dependencies, validated on admission.
    flags: tuple[str, ...] = ()

    def merged(self, other: PluginContributions) -> PluginContributions:
        """This declaration followed by `other` (another face of the same plugin): tuple fields
        concatenate, `config` may be declared by one face only."""
        if self.config is not None and other.config is not None:
            raise ValueError("config is declared by more than one face")
        values: dict[str, object] = {}
        for f in fields(self):
            mine, theirs = getattr(self, f.name), getattr(other, f.name)
            values[f.name] = mine + theirs if isinstance(mine, tuple) else mine or theirs
        return PluginContributions(**values)  # pyright: ignore[reportArgumentType]

    def hooks(self, point: HookPoint) -> tuple[GraphHook, ...]:
        return getattr(self, point)

    def as_records(self, plugin: str) -> tuple[Contribution, ...]:
        """The same facts as attribution records, spelled the way the manifest declares them
        (`ava plugins inspect` shows these; the manifest check compares them)."""
        return (
            *self._runtime_records(plugin),
            *self._data_records(plugin),
            *self._sdk_records(plugin),
        )

    def _runtime_records(self, plugin: str) -> tuple[Contribution, ...]:
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
        return (*sections, *notes, *hooks, *state)

    def _data_records(self, plugin: str) -> tuple[Contribution, ...]:
        metrics = (Contribution("metrics", spec.name, plugin, spec.title) for spec in self.metrics)
        widgets = (
            Contribution("inspectWidgets", spec.id, plugin, spec.kind)
            for spec in self.inspect_widgets
        )
        providers = (
            Contribution("providers", p.binding.prefix, plugin, p.binding.display_name)
            for p in self.providers
        )
        return (*metrics, *widgets, *providers)

    def _sdk_records(self, plugin: str) -> tuple[Contribution, ...]:
        namespaces = (
            Contribution("sdkNamespaces", ns.name, plugin, getattr(ns.module, "__name__", ""))
            for ns in self.sdk_namespaces
        )
        members = (
            Contribution(
                "sdkMembers",
                f"{m.namespace}.{m.name}",
                plugin,
                f"{getattr(m.fn, '__module__', '?')}.{getattr(m.fn, '__qualname__', m.fn)}",
            )
            for m in self.sdk_members
        )
        promoted = (*(ns.name for ns in self.sdk_namespaces if ns.expand), *self.sdk_expansions)
        expansions = (
            Contribution("sdkExpansions", path, plugin, "expanded ahead of the framework list")
            for path in promoted
        )
        wraps = (
            Contribution(
                "sdkWraps",
                w.target,
                plugin,
                f"{getattr(w.wrapper, '__module__', '?')}.{getattr(w.wrapper, '__qualname__', w.wrapper)}",
            )
            for w in self.sdk_wraps
        )
        sources = (
            Contribution(
                "skillSources",
                getattr(p, "__name__", repr(p)),
                plugin,
                getattr(p, "__module__", "?"),
            )
            for p in self.skill_sources
        )
        config: tuple[Contribution, ...] = ()
        if self.config is not None:
            fields_text = ", ".join(self.config.model_fields) or "<none>"
            config = (
                Contribution("config", self.config.__name__, plugin, f"fields: {fields_text}"),
            )
        return (*namespaces, *members, *expansions, *wraps, *sources, *config)


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

    def sdk_wraps(self) -> Iterator[tuple[str, SdkWrap]]:
        """(plugin, wrap) in load order, then declaration order."""
        for plugin, contributions in self.plugins:
            for wrap in contributions.sdk_wraps:
                yield plugin, wrap

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
