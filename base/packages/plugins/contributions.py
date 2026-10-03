"""The attribution record: one fact about what a plugin contributes.

`ava plugins inspect` reads these off `PluginContributions.as_records(plugin)`
(`base/packages/plugins/extensions.py`), and every record is keyed by the same identifiers the
`ava-plugin.json` manifest declares (`base/packages/plugins/manifest.py:CONTRIBUTION_KEYS`), so
declared and delivered are directly comparable. There is no ledger to write to: a plugin declares, the
registry holds the declaration, and the record is derived from it.

The record is what a plugin DECLARED, never what ran. What ran is
`base/packages/plugins/activation.py`: one event per firing keyed by the same
`(plugin, surface, identifier)` triple, so the two views join on three strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Surface ids. The first five are also `ava-plugin.json` contribution keys, so a
# manifest declaration and a registration meet on the same string; the rest are
# surfaces the spec-v2 grammar has no key for (see `agent/extensions/catalog.py`,
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
    "metrics",
    "inspectWidgets",
]


@dataclass(frozen=True)
class Contribution:
    """One declared contribution, attributed to its plugin.

    - surface: which extension surface was used.
    - identifier: what was registered, spelled the way the manifest declares it
      for that surface — a hook point (`"before_llm"`), an `ava` namespace
      (`"cwd"`), a wrap target (`"files.read"`), a state channel key
      (`"ava_code__cwd"`), a section / provider function name.
    - plugin: the plugin that declared it.
    - detail: one line of specifics for a reader — the hook class, the wrapper
      function, the field annotation. Free text, never parsed.
    """

    surface: SurfaceId
    identifier: str
    plugin: str
    detail: str
