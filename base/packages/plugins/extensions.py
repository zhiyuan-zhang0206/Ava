from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from base.api_contracts.metric_spec import MetricSpec
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_contract import DecisionProviderContribution, ProviderContribution
from base.packages.plugins.contributions import Contribution
from base.packages.plugins.surface.inspector import InspectWidgetSpec

NoteBuilder = Callable[..., object | None]


class SectionFn(Protocol):
    @property
    def __name__(self) -> str: ...

    def __call__(self, slices: AgentSlices, /, *, catalog: ModelCatalog) -> str: ...


HookPoint = Literal["after_init", "before_llm", "before_exec", "after_exec"]
HOOK_POINTS: tuple[HookPoint, ...] = ("after_init", "before_llm", "before_exec", "after_exec")


class GraphHook(Protocol):
    @property
    def name(self) -> str: ...

    async def __call__(self, state: Any, runtime: Any, config: Any, /) -> dict[str, Any] | None: ...


PLUGIN_WRITABLE_BASE_FIELDS: frozenset[str] = frozenset({"messages"})

SDK_WRITTEN_BASE_FIELDS: frozenset[str] = frozenset({"security_findings", "attach", "skill_reads"})

DEFAULT_NOTE_RANK = 100


@dataclass(frozen=True)
class ContextNote:
    build: NoteBuilder
    on_fork: bool = False
    rank: int = DEFAULT_NOTE_RANK


@dataclass(frozen=True)
class SdkNamespace:
    name: str
    module: ModuleType | SimpleNamespace
    expand: bool = False


@dataclass(frozen=True)
class SdkMember:
    namespace: str
    name: str
    fn: Callable[..., Any]


@dataclass(frozen=True)
class SdkWrap:
    target: str
    wrapper: Callable[..., Any]


# ava:ref base/packages/plugins/docs/runtime-contributions.ava.okf.md#immutable-declarations
@dataclass(frozen=True)
class PluginContributions:
    system_prompt_sections: tuple[SectionFn, ...] = ()
    compact_reminder_sections: tuple[SectionFn, ...] = ()
    context_notes: tuple[ContextNote, ...] = ()
    after_init: tuple[GraphHook, ...] = ()
    before_llm: tuple[GraphHook, ...] = ()
    before_exec: tuple[GraphHook, ...] = ()
    after_exec: tuple[GraphHook, ...] = ()
    state: tuple[type[BaseModel], ...] = ()
    metrics: tuple[MetricSpec, ...] = ()
    inspect_widgets: tuple[InspectWidgetSpec, ...] = ()
    providers: tuple[ProviderContribution, ...] = ()
    decision_providers: tuple[DecisionProviderContribution, ...] = ()
    sdk_namespaces: tuple[SdkNamespace, ...] = ()
    sdk_members: tuple[SdkMember, ...] = ()
    sdk_wraps: tuple[SdkWrap, ...] = ()
    skill_sources: tuple[Callable[[], list[Path]], ...] = ()
    sdk_expansions: tuple[str, ...] = ()
    config: type[BaseModel] | None = None
    flags: tuple[str, ...] = ()

    def merged(self, other: PluginContributions) -> PluginContributions:
        if self.config is not None and other.config is not None:
            raise ValueError("config is declared by more than one face")
        values: dict[str, Any] = {}
        for f in fields(self):
            mine, theirs = getattr(self, f.name), getattr(other, f.name)
            values[f.name] = mine + theirs if isinstance(mine, tuple) else mine or theirs
        return PluginContributions(**values)

    def hooks(self, point: HookPoint) -> tuple[GraphHook, ...]:
        return getattr(self, point)

    def as_records(self, plugin: str) -> tuple[Contribution, ...]:
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
        reminders = (
            Contribution("compactReminderSections", fn.__name__, plugin, fn.__module__)
            for fn in self.compact_reminder_sections
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
        return (*sections, *reminders, *notes, *hooks, *state)

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
        decision_providers = (
            Contribution("decisionProviders", p.prefix, plugin, p.display_name)
            for p in self.decision_providers
        )
        return (*metrics, *widgets, *providers, *decision_providers)

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


# ava:ref base/packages/plugins/docs/runtime-contributions.ava.okf.md#immutable-declarations
@dataclass(frozen=True)
class ExtensionRegistry:
    plugins: tuple[tuple[str, PluginContributions], ...] = ()

    def system_prompt_sections(self) -> Iterator[tuple[str, SectionFn]]:
        for plugin, contributions in self.plugins:
            for fn in contributions.system_prompt_sections:
                yield plugin, fn

    def context_notes(self) -> Iterator[tuple[str, ContextNote]]:
        for plugin, contributions in self.plugins:
            for note in contributions.context_notes:
                yield plugin, note

    def compact_reminder_sections(self) -> Iterator[tuple[str, SectionFn]]:
        for plugin, contributions in self.plugins:
            for fn in contributions.compact_reminder_sections:
                yield plugin, fn

    def hooks(self, point: HookPoint) -> Iterator[tuple[str, GraphHook]]:
        for plugin, contributions in self.plugins:
            for hook in contributions.hooks(point):
                yield plugin, hook

    def state_classes(self) -> Iterator[tuple[str, type[BaseModel]]]:
        for plugin, contributions in self.plugins:
            for cls in contributions.state:
                yield plugin, cls

    def sdk_wraps(self) -> Iterator[tuple[str, SdkWrap]]:
        for plugin, contributions in self.plugins:
            for wrap in contributions.sdk_wraps:
                yield plugin, wrap

    def metrics(self) -> Iterator[MetricSpec]:
        for _plugin, contributions in self.plugins:
            yield from contributions.metrics

    def inspect_widgets(self) -> Iterator[InspectWidgetSpec]:
        for _plugin, contributions in self.plugins:
            yield from contributions.inspect_widgets

    def records(self, plugin: str) -> tuple[Contribution, ...]:
        return tuple(r for name, c in self.plugins if name == plugin for r in c.as_records(plugin))


EMPTY = ExtensionRegistry()
