"""What a plugin can extend, and what each installed plugin actually extended —
the computation behind `ava plugins inspect`.

Two halves, deliberately different in kind:

- **Surfaces** — the framework's extension points. The list of surfaces and the
  one-line note on each are the only hand-written facts here; every signature is
  rendered from the live `register_*` / `contribute()` object at call time, so a changed
  parameter list shows up in the catalog without anyone editing it.
- **Contributions** — what the plugins on THIS machine registered, read off the
  attribution ledger (`base.packages.plugins.contributions`) that every `register_*`
  entry point writes to. Facts, not documentation: a plugin appears here because
  its import called the entry point, not because someone wrote it down.

Reading the second half requires the plugins to be loaded, and loading them is
importing them — the catalog runs `load_extensions()` in the calling process,
exactly as an agent boot does. A DISABLED plugin is therefore listed with its
enable-state and nothing else: nothing imported it, so there is no registration
fact to report, and inventing one from its source would be the docs-drift this
catalog exists to replace.

`declared_vs_registered` is the same computation plugin-spec-v2 S3 turns into a
load-time gate (`conventions/plugin-spec-v2.md`). Here it only reports:
`declared-not-registered` is the warning shape (a manifest promising a surface
the code never touched), `registered-not-declared` the flagged one (a surface the
manifest failed to mention). Surfaces the manifest grammar cannot express are
reported as `undeclarable` rather than as drift — flagging them would train a
reader to ignore the diff.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from base.packages.plugins import contributions as contribution_ledger
from base.packages.plugins import enable_config
from base.packages.plugins.contributions import Contribution
from base.packages.plugins.manifest import CONTRIBUTION_KEYS, PluginManifest, load_manifest
from base.packages.skills import names

# ── surfaces ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Surface:
    """One extension point of the framework.

    - id: the ledger surface id; equal to the `ava-plugin.json` contribution key
      when the manifest grammar has one for it.
    - entry_points: `module:attr` paths of the functions a plugin calls. Resolved
      live for the rendered signature, so an unresolvable path is a hard failure
      rather than a stale line.
    - manifest_key: the `contributions` key that declares this surface, or None
      when the spec has no way to declare it.
    - protocol: a second signature a contributor must satisfy (the `Hook` call
      contract), when the registered object is more than a plain callable.
    - note: what registering does, in one line.
    """

    id: str
    entry_points: tuple[str, ...]
    manifest_key: str | None
    protocol: str | None
    note: str


SURFACES: tuple[Surface, ...] = (
    Surface(
        id="hooks",
        entry_points=("base.packages.plugins.extensions:PluginContributions",),
        manifest_key="hooks",
        protocol="agent.hooks.Hook.__call__"
        "(state: AgentState, runtime: Runtime[AvaContext], config: RunnableConfig, /)"
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
        entry_points=("base.packages.plugins.extensions:PluginContributions",),
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
        entry_points=("ava:register_namespace",),
        manifest_key="sdkNamespaces",
        protocol=None,
        note="adds ava.<name> for the agent to call; conflicts with an existing name are refused",
    ),
    Surface(
        id="sdkMembers",
        entry_points=("ava:register_namespace_member",),
        manifest_key=None,
        protocol=None,
        note=(
            "hangs one callable on an existing namespace (ava.self.set_label); preferred over "
            "a new top-level name, and its docstring is what the agent reads"
        ),
    ),
    Surface(
        id="sdkExpansions",
        entry_points=("ava:register_sdk_expand",),
        manifest_key=None,
        protocol=None,
        note=(
            "promotes a dotted ava path into the system prompt's expanded SDK reference, "
            "ahead of the framework list"
        ),
    ),
    Surface(
        id="sdkWraps",
        entry_points=("ava:extend.wrap",),
        manifest_key="sdkWraps",
        protocol="wrapper(inner: Callable, *args, **kwargs) -> Any",
        note=(
            "installs a layer around an existing ava callable; layers stack in plugin load "
            "order and `ava.extend.stack(target)` shows the whole chain"
        ),
    ),
    Surface(
        id="systemPromptSections",
        entry_points=("base.packages.plugins.extensions:PluginContributions",),
        manifest_key="systemPromptSections",
        protocol="section(slices: AgentSlices) -> str",
        note=(
            "`system_prompt_sections` of what `contribute()` returns: appended to the system "
            "prompt after the framework's own; an empty return contributes nothing"
        ),
    ),
    Surface(
        id="contextNotes",
        entry_points=("base.packages.plugins.extensions:ContextNote",),
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
        entry_points=("base.packages.plugins.extensions:PluginContributions",),
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
        entry_points=("base.packages.plugins.extensions:PluginContributions",),
        manifest_key="inspectWidgets",
        protocol="InspectWidgetSpec",
        note=(
            "`inspect_widgets` of what the plugin's `inspector.py` `contribute()` returns: a "
            "closed-set widget the console renders in the agent inspector from kernel-resolved data"
        ),
    ),
    Surface(
        id="skillSources",
        entry_points=("ava.skills:register_skill_source",),
        manifest_key=None,
        protocol="provider() -> list[Path]",
        note=(
            "contributes skill roots computed at scan time (project-local skills that follow "
            "the agent's cwd); provider roots scan last, so they override same-named builtins"
        ),
    ),
    Surface(
        id="config",
        entry_points=("base.packages.plugins.config_registration:register_plugin_config",),
        manifest_key="config",
        protocol=None,
        note=(
            "a frozen BaseModel bound once from $AVA_HOME/configs/<plugin>/config.json and "
            "read as ava._settings.plugins.<plugin>; schema drift points at `ava plugins update`"
        ),
    ),
)

# Manifest contribution keys with no runtime registry behind them: they declare
# install-time facts (files on disk, a service roster entry), so a diff can only
# say "declared" — never "registered". Kept as the complement of the mapped keys
# so a new CONTRIBUTION_KEYS entry cannot silently fall out of the catalog.
DECLARATION_ONLY_KEYS: frozenset[str] = frozenset(CONTRIBUTION_KEYS) - {
    s.manifest_key for s in SURFACES if s.manifest_key is not None
}


class _SourceText:
    """An annotation that renders as itself.

    A module under `from __future__ import annotations` hands `inspect` its
    annotations as strings, which `Signature.__str__` renders through `repr` —
    `hook: 'Hook'`. Substituting this makes the rendered signature read the way
    the source does, uniformly across modules that do and do not defer
    annotations. `eval_str=True` would unquote them too, but by resolving the
    real objects: `Callable[[NoteBuilder], NoteBuilder]` comes back fully
    expanded down to `langchain_core.messages.human.HumanMessage`, which is the
    same contract spelled unreadably.
    """

    def __init__(self, text: str) -> None:
        self._text = text

    def __repr__(self) -> str:
        return self._text


def _unquoted(annotation: Any) -> Any:
    return _SourceText(annotation) if isinstance(annotation, str) else annotation


def entry_point_signature(entry_point: str) -> str:
    """`agent.hooks.make_hook_runner(hook_name, default_next, hooks) -> ...` — rendered from the
    live object, so the catalog cannot drift from the code it describes.

    Raises:
        ModuleNotFoundError / AttributeError: the entry point moved and `SURFACES`
            was not updated — a catalog that describes a surface nobody can call
            is worse than a crash.
    """
    module_path, _, attr_path = entry_point.partition(":")
    obj: Any = importlib.import_module(module_path)
    for segment in attr_path.split("."):
        obj = getattr(obj, segment)
    signature = inspect.signature(obj)
    signature = signature.replace(
        parameters=[
            p.replace(annotation=_unquoted(p.annotation)) for p in signature.parameters.values()
        ],
        return_annotation=_unquoted(signature.return_annotation),
    )
    return f"{entry_point.replace(':', '.')}{signature}"


# ── the per-machine composition ─────────────────────────────────────────


@dataclass(frozen=True)
class DiffEntry:
    """One line of the declared-vs-registered comparison.

    `status` is one of `ok`, `declared-not-registered` (the manifest promises a
    surface no registration reached), `registered-not-declared` (a registration
    the manifest never mentioned).
    """

    surface: str
    identifier: str
    status: str


@dataclass(frozen=True)
class PluginView:
    """One plugin as this machine sees it.

    `contributions` is empty for a disabled plugin — nothing imported it. Read
    `enabled` before reading a contribution count as "this plugin registers
    nothing".
    """

    name: str
    enabled: bool
    builtin: bool
    directory: Path
    description: str
    contributions: tuple[Contribution, ...]
    manifest: PluginManifest | None

    def surface_counts(self) -> dict[str, int]:
        """Surface id -> number of contributions, in `SURFACES` order."""
        counts: dict[str, int] = {}
        for surface in SURFACES:
            n = sum(1 for c in self.contributions if c.surface == surface.id)
            if n:
                counts[surface.id] = n
        return counts


@dataclass(frozen=True)
class Catalog:
    """The whole machine: the framework's surfaces plus every installed plugin."""

    surfaces: tuple[Surface, ...]
    plugins: tuple[PluginView, ...]

    def plugin(self, name: str) -> PluginView:
        """One plugin by name, dash/underscore folded like `plugins_config.load`.

        Raises:
            UnknownPlugin: no plugin of that name is installed on this machine.
        """
        known = {p.name for p in self.plugins}
        resolved = names.find(name, known) or name
        for view in self.plugins:
            if view.name == resolved:
                return view
        raise UnknownPlugin(
            f"no plugin named {name!r} is installed on this machine "
            f"(installed: {', '.join(sorted(known)) or '<none>'})"
        )


class UnknownPlugin(Exception):  # noqa: N818 — named like its siblings in base.packages.plugins.enable_config (DanglingPlugin / DuplicatePlugin), which describe the plugin condition rather than carry an Error suffix
    """`ava plugins inspect <name>` named a plugin that is not installed here."""


def build_catalog() -> Catalog:
    """Load this machine's enabled plugins and read back what they registered.

    Importing the plugins is the point — registration facts exist only once
    `plugin.py` has run — so this has the side effects of an agent boot's
    extension load: SDK namespaces appear on `ava`, wraps are installed, plugin
    config is bound from disk. Call it from a short-lived process (the CLI), not
    from one that must keep a pristine `ava`.
    """
    from agent.extensions import load_extensions
    from agent.extensions.registry import declarations

    config = load_extensions()
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
                # An undiscovered-at-load-time plugin cannot happen (`load` merges
                # every known name in), so a missing entry means the config and the
                # filesystem disagree — report it as disabled rather than guess.
                enabled=entry is not None and entry.enabled,
                builtin=directory.is_relative_to(repo_plugins),
                directory=directory,
                description=enable_config.parse_description(directory / "plugin.py"),
                contributions=(
                    *contribution_ledger.contributions_of(name),
                    *(declared[name].as_records(name) if name in declared else ()),
                    *data_records.get(name, ()),
                ),
                manifest=load_manifest(directory),
            )
        )
    return Catalog(surfaces=SURFACES, plugins=tuple(views))


def _data_records() -> dict[str, tuple[Contribution, ...]]:
    """What each enabled plugin's data faces (`metrics.py`, `inspector.py`) declare, as records.

    Loaded here only to be described — the processes that serve these surfaces (the gateway, the
    Grafana supply) load them on their own, into their own registries.
    """
    from agent.extensions import _enabled_plugin_dirs, _pkg_of
    from ava.sdk_surface.plugin_loader import safe_load_plugin_module
    from base.packages.plugins import data_registry

    records: dict[str, tuple[Contribution, ...]] = {}
    for name, directory in _enabled_plugin_dirs():
        found: list[Contribution] = []
        for face in ("metrics", "inspector"):
            path = directory / f"{face}.py"
            if not path.is_file():
                continue
            module = safe_load_plugin_module(path, name=name, pkg=_pkg_of(directory), module=face)
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


def declared_vs_registered(view: PluginView) -> tuple[DiffEntry, ...]:
    """Compare a plugin's manifest declarations against what it registered.

    Empty when the plugin ships no manifest — there is nothing to compare, which
    is not the same as agreement. Only surfaces with both a manifest key and a
    runtime registry take part; `DECLARATION_ONLY_KEYS` and the surfaces with no
    manifest key are reported separately by the caller.
    """
    if view.manifest is None:
        return ()
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


def _declared_identifiers(manifest: PluginManifest, key: str) -> set[str]:
    """The identifiers a manifest declares under one contribution key.

    `contributions.config` is an object rather than a list — a plugin either
    declares a config or does not — so it folds to the single identifier
    `"<declared>"`, which the registered side matches by folding its config class
    name the same way. Comparing class names would make renaming a private class
    read as drift.
    """
    declared = manifest.contributions.get(key)
    if declared is None:
        return set()
    if key == "config":
        return {"<declared>"}
    assert isinstance(declared, list), f"contributions.{key} validates to a list"  # noqa: S101
    return {str(item) for item in cast(list[object], declared)}


def _registered_identifiers(view: PluginView, surface: Surface) -> set[str]:
    """One surface's registered side, folded the same way its declared side is."""
    identifiers = {c.identifier for c in view.contributions if c.surface == surface.id}
    if surface.id == "config":
        return {"<declared>"} if identifiers else set()
    return identifiers
