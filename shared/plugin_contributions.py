"""The attribution ledger every plugin registration writes to — the read model
behind `ava plugins inspect`.

Each extension surface keeps its own registry shaped for its own consumer: the
hook runner wants a list of `Hook` instances, LangGraph wants annotated state
fields, the prompt builder wants section callables. None of them carry the one
fact an operator or a plugin-authoring agent asks for — *which plugin put this
here* — and three of them cannot carry it without changing a shape the framework
reads on every turn. So attribution is recorded here instead, one line beside
each registry mutation, keyed by the same identifiers the `ava-plugin.json`
manifest declares (`shared/plugin_manifest.py:CONTRIBUTION_KEYS`) so declared and
registered are directly comparable.

**Only registrations made inside a `PluginContext` are recorded.** The framework
registers its own hooks and prompt sections through the same entry points, and a
test may wrap an `ava` target with no plugin in scope; neither is a plugin
contribution, and recording them would make the ledger's content depend on
whether a reload had happened yet (framework registrations happen once at module
import, plugin ones on every `_load_extensions`). Gating on the ContextVar makes
the ledger exactly parallel to the plugin tails of the registries it shadows:
`agent.state.clear_plugin_registrations` clears it, and the plugin imports that
follow rebuild it.

The ledger is a record of what was REGISTERED, never of what ran. What ran is
`shared/plugin_activation.py`, which hangs off these records rather than
replacing them: it emits one event per firing keyed by the same
`(plugin, surface, identifier)` triple, under the same
only-inside-a-`PluginContext` gate, so the two views join on three strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from shared.plugin_context import current_plugin_name

# Surface ids. The first five are also `ava-plugin.json` contribution keys, so a
# manifest declaration and a registration meet on the same string; the rest are
# surfaces the spec-v2 grammar has no key for (see `agent/plugin_catalog.py`,
# which reports them as undeclarable rather than as undeclared drift).
SurfaceId = Literal[
    "hooks",
    "sdkNamespaces",
    "sdkWraps",
    "systemPromptSections",
    "config",
    "sdkMembers",
    "sdkExpansions",
    "state",
    "contextNotes",
    "skillSources",
]


@dataclass(frozen=True)
class Contribution:
    """One registration, attributed to the plugin whose import made it.

    - surface: which extension surface was used.
    - identifier: what was registered, spelled the way the manifest declares it
      for that surface — a hook point (`"before_llm"`), an `ava` namespace
      (`"cwd"`), a wrap target (`"files.read"`), a state channel key
      (`"ava_code__cwd"`), a section / provider function name.
    - plugin: the plugin that registered it.
    - detail: one line of specifics for a reader — the hook class, the wrapper
      function, the field annotation. Free text, never parsed.
    """

    surface: SurfaceId
    identifier: str
    plugin: str
    detail: str


_CONTRIBUTIONS: list[Contribution] = []


def record(surface: SurfaceId, identifier: str, *, detail: str = "") -> None:
    """Record one plugin registration; a no-op outside a `PluginContext`.

    Called from inside each `register_*` entry point, next to the registry
    mutation it shadows, so the two cannot be written apart.
    """
    plugin = current_plugin_name()
    if plugin is None:
        return
    _CONTRIBUTIONS.append(
        Contribution(surface=surface, identifier=identifier, plugin=plugin, detail=detail)
    )


def contributions() -> tuple[Contribution, ...]:
    """Every recorded contribution, in registration (= plugin load) order."""
    return tuple(_CONTRIBUTIONS)


def contributions_of(plugin: str) -> tuple[Contribution, ...]:
    """One plugin's contributions, in registration order."""
    return tuple(c for c in _CONTRIBUTIONS if c.plugin == plugin)


def clear() -> None:
    """Drop the ledger, so the next plugin load rebuilds it from empty. Called by
    `agent.state.clear_plugin_registrations` alongside the registries it shadows."""
    _CONTRIBUTIONS.clear()
