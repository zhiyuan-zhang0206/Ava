from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from base.lm.provider_api import DecisionProviderContribution, ProviderContribution
from base.packages.plugins import enable_config
from base.packages.plugins.contributions import Contribution
from base.packages.plugins.extensions import (
    ContextNote,
    PluginContributions,
    SdkMember,
    SdkNamespace,
    SdkWrap,
)
from base.packages.plugins.loading.discovery import enabled_plugin_dirs, pkg_of
from base.packages.plugins.manifest import CONTRIBUTION_KEYS, PluginManifest, load_manifest
from base.packages.skills import names


@dataclass(frozen=True)
class Surface:
    id: str
    entry_points: tuple[type, ...]
    manifest_key: str | None
    protocol: str | None
    note: str


SURFACES: tuple[Surface, ...] = (
    Surface(
        id="hooks",
        entry_points=(PluginContributions,),
        manifest_key="hooks",
        protocol="agent.hooks.Hook.__call__"
        "(state: AgentState, runtime: Runtime[MeivaContext], config: RunnableConfig, /)"
        " -> dict | None",
        note=(
            "`after_init` / `before_llm` / `before_exec` / `after_exec` of what `contribute()` "
            "returns: a Hook instance runs at that graph edge; the returned dict is a state "
            "update (key 'goto' overrides routing), None is a no-op, and two hooks writing one "
            "reducerless key in a pass is a hard error"
        ),
    ),
    Surface(
        id="state",
        entry_points=(PluginContributions,),
        manifest_key=None,
        protocol=None,
        note=(
            "`state` of what `contribute()` returns: fields of each BaseModel become LangGraph "
            "channels named <plugin>__<field>, read/written through a "
            "`PluginStateHandle(cls, plugin)` inside an exec turn; 'messages' is the only base "
            "channel a plugin may declare"
        ),
    ),
    Surface(
        id="sdkNamespaces",
        entry_points=(SdkNamespace,),
        manifest_key="sdkNamespaces",
        protocol=None,
        note=(
            "`sdk_namespaces` of what `plugin.py`'s `contribute()` returns: adds meiva.<name> for the "
            "agent to call (`expand=True` also promotes it into the prompt's expanded SDK "
            "reference); a name that conflicts with an existing one refuses the plugin"
        ),
    ),
    Surface(
        id="sdkMembers",
        entry_points=(SdkMember,),
        manifest_key=None,
        protocol=None,
        note=(
            "`sdk_members`: hangs one callable on an existing namespace (meiva.self.set_label); preferred over "
            "a new top-level name, and its docstring is what the agent reads"
        ),
    ),
    Surface(
        id="sdkExpansions",
        entry_points=(PluginContributions,),
        manifest_key=None,
        protocol=None,
        note=(
            "`sdk_expansions`: promotes a dotted meiva path into the system prompt's expanded SDK reference, "
            "ahead of the framework list"
        ),
    ),
    Surface(
        id="sdkWraps",
        entry_points=(SdkWrap,),
        manifest_key="sdkWraps",
        protocol="wrapper(inner: Callable, *args, **kwargs) -> Any",
        note=(
            "`sdk_wraps`: installs a layer around an existing meiva callable; layers stack in plugin load "
            "order and `meiva.extend.stack(target)` shows the whole chain"
        ),
    ),
    Surface(
        id="systemPromptSections",
        entry_points=(PluginContributions,),
        manifest_key="systemPromptSections",
        protocol="section(slices: AgentSlices, *, catalog: ModelCatalog) -> str",
        note=(
            "`system_prompt_sections` of what `contribute()` returns: appended to the system "
            "prompt after the framework's own; an empty return contributes nothing"
        ),
    ),
    Surface(
        id="compactReminderSections",
        entry_points=(PluginContributions,),
        manifest_key="compactReminderSections",
        protocol="section(slices: AgentSlices, *, catalog: ModelCatalog) -> str",
        note=(
            "`compact_reminder_sections`: short, side-effect-free instructions appended to "
            "the soft compact reminder, once per context window; empty text opts out"
        ),
    ),
    Surface(
        id="contextNotes",
        entry_points=(ContextNote,),
        manifest_key=None,
        protocol="note(slices: AgentSlices) -> HumanMessage | None",
        note=(
            "`context_notes` of what `contribute()` returns: a standing note laid down whenever "
            "a context window is established; rank orders the head, on_fork also grafts it onto "
            "a forked agent"
        ),
    ),
    Surface(
        id="metrics",
        entry_points=(PluginContributions,),
        manifest_key="metrics",
        protocol="MetricSpec",
        note=(
            "`metrics` of what the plugin's `metrics.py` `contribute()` returns: a query over the "
            "event stream rendered on the Grafana dashboard and/or the agent inspector; names are "
            "global across plugins and every query is validated before the plugin is admitted"
        ),
    ),
    Surface(
        id="inspectWidgets",
        entry_points=(PluginContributions,),
        manifest_key="inspectWidgets",
        protocol="InspectWidgetSpec",
        note=(
            "`inspect_widgets` of what the plugin's `inspector.py` `contribute()` returns: a "
            "closed-set widget the console renders in the agent inspector from kernel-resolved data"
        ),
    ),
    Surface(
        id="providers",
        entry_points=(ProviderContribution,),
        manifest_key="providers",
        protocol=None,
        note=(
            "`providers` of what the plugin's `provider.py` `contribute()` returns: a dispatch "
            "binding, the chat-model rows it owns and their live prices, installed into the "
            "process's model catalog by the provider loader; a prefix that collides with another "
            "provider is a hard error, not a precedence order"
        ),
    ),
    Surface(
        id="decisionProviders",
        entry_points=(DecisionProviderContribution,),
        manifest_key="decisionProviders",
        protocol=None,
        note=(
            "`decision_providers` of what the plugin's `provider.py` `contribute()` returns: a "
            "decision-model provider (typed questions answered with probabilities, never text), "
            "its model rows and live prices, installed into the catalog's decision tables, which "
            "no chat path can reach; a prefix that collides with another plugin's provider is a "
            "hard error"
        ),
    ),
    Surface(
        id="skillSources",
        entry_points=(PluginContributions,),
        manifest_key=None,
        protocol="provider() -> list[Path]",
        note=(
            "`skill_sources`: contributes skill roots computed at scan time (project-local skills that follow "
            "the agent's cwd); provider roots scan last, so they override same-named builtins"
        ),
    ),
    Surface(
        id="config",
        entry_points=(PluginContributions,),
        manifest_key="config",
        protocol=None,
        note=(
            "`config`: a frozen BaseModel bound once from $MEIVA_HOME/configs/<plugin>/config.json and "
            "read as meiva.sdk_surface.settings.plugins.<plugin>; schema drift points at `meiva plugins update`"
        ),
    ),
)

DECLARATION_ONLY_KEYS: frozenset[str] = frozenset(CONTRIBUTION_KEYS) - {
    s.manifest_key for s in SURFACES if s.manifest_key is not None
}


class _SourceText:
    def __init__(self, text: str) -> None:
        self._text = text

    def __repr__(self) -> str:
        return self._text


def _unquoted(annotation: Any) -> Any:
    return _SourceText(annotation) if isinstance(annotation, str) else annotation


# meiva:ref agent/extensions/docs/catalog.meiva.okf.md#catalog-reports-load-facts
def entry_point_signature(entry_point: type) -> str:
    signature = inspect.signature(entry_point)
    signature = signature.replace(
        parameters=[
            p.replace(annotation=_unquoted(p.annotation)) for p in signature.parameters.values()
        ],
        return_annotation=_unquoted(signature.return_annotation),
    )
    return f"{entry_point.__module__}.{entry_point.__qualname__}{signature}"


@dataclass(frozen=True)
class DiffEntry:
    surface: str
    identifier: str
    status: str


@dataclass(frozen=True)
class PluginView:
    name: str
    enabled: bool
    builtin: bool
    directory: Path
    description: str
    contributions: tuple[Contribution, ...]
    manifest: PluginManifest

    def surface_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for surface in SURFACES:
            n = sum(1 for c in self.contributions if c.surface == surface.id)
            if n:
                counts[surface.id] = n
        return counts


@dataclass(frozen=True)
class Catalog:
    surfaces: tuple[Surface, ...]
    plugins: tuple[PluginView, ...]

    def plugin(self, name: str) -> PluginView:
        # meiva:ref agent/extensions/docs/catalog.meiva.okf.md#catalog-reports-load-facts
        known = {p.name for p in self.plugins}
        resolved = names.find(name, known) or name
        for view in self.plugins:
            if view.name == resolved:
                return view
        raise UnknownPluginError(
            f"no plugin named {name!r} is installed on this machine "
            f"(installed: {', '.join(sorted(known)) or '<none>'})"
        )


class UnknownPluginError(Exception):
    pass


# meiva:ref agent/extensions/docs/catalog.meiva.okf.md#catalog-reports-load-facts
def build_catalog() -> Catalog:
    from agent.state import plugin_state_schema
    from base.packages.plugins.loading.registry import declarations
    from meiva.sdk_surface.extensions import load_extensions

    config = load_extensions(validate_state=plugin_state_schema).config
    declared = {name: contributions for name, _dir, contributions in declarations()}
    data_records = _data_records()
    discovered = enable_config.installed_plugin_dirs()
    repo_plugins = _repo_plugins_dir()

    views: list[PluginView] = []
    for name in sorted(discovered):
        directory = discovered[name]
        entry = config.plugins.get(name)
        views.append(
            PluginView(
                name=name,
                enabled=entry is not None and entry.enabled,
                builtin=directory.is_relative_to(repo_plugins),
                directory=directory,
                description=enable_config.parse_description(directory / "plugin.py"),
                contributions=(
                    *(declared[name].as_records(name) if name in declared else ()),
                    *data_records.get(name, ()),
                ),
                manifest=load_manifest(directory),
            )
        )
    return Catalog(surfaces=SURFACES, plugins=tuple(views))


def _data_records() -> dict[str, tuple[Contribution, ...]]:
    from base.packages.plugins import data_registry
    from base.packages.plugins.loading.plugin_loader import safe_load_plugin_module

    records: dict[str, tuple[Contribution, ...]] = {}
    for name, directory in enabled_plugin_dirs():
        found: list[Contribution] = []
        for face in ("metrics", "inspector", "provider"):
            path = directory / f"{face}.py"
            if not path.is_file():
                continue
            module = safe_load_plugin_module(path, name=name, pkg=pkg_of(directory), module=face)
            if module is None:
                continue
            declared = data_registry.load_declaration(name, lambda module=module: module)
            if declared is not None:
                found.extend(declared.contributions.as_records(name))
        if found:
            records[name] = tuple(found)
    return records


def _repo_plugins_dir() -> Path:
    from base import paths

    return paths.repo_plugins_dir()


# meiva:ref agent/extensions/docs/catalog.meiva.okf.md#manifest-diff-scope
def declared_vs_registered(view: PluginView) -> tuple[DiffEntry, ...]:
    entries: list[DiffEntry] = []
    for surface in SURFACES:
        if surface.manifest_key is None:
            continue
        declared = _declared_identifiers(view.manifest, surface.manifest_key)
        registered = _registered_identifiers(view, surface)
        for identifier in sorted(declared | registered):
            if identifier in declared and identifier in registered:
                status = "ok"
            elif identifier in declared:
                status = "declared-not-registered"
            else:
                status = "registered-not-declared"
            entries.append(DiffEntry(surface=surface.id, identifier=identifier, status=status))
    return tuple(entries)


# meiva:ref agent/extensions/docs/catalog.meiva.okf.md#manifest-diff-scope
def _declared_identifiers(manifest: PluginManifest, key: str) -> set[str]:
    declared = manifest.contributions.get(key)
    if declared is None:
        return set()
    if key == "config":
        return {"<declared>"}
    if not isinstance(declared, list):
        raise TypeError(f"contributions.{key} must be a validated list")
    return {str(item) for item in cast(list[object], declared)}


def _registered_identifiers(view: PluginView, surface: Surface) -> set[str]:
    identifiers = {c.identifier for c in view.contributions if c.surface == surface.id}
    if surface.id == "config":
        return {"<declared>"} if identifiers else set()
    return identifiers
