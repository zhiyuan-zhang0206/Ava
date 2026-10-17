from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SurfaceId = Literal[
    "hooks",
    "sdkNamespaces",
    "sdkWraps",
    "systemPromptSections",
    "compactReminderSections",
    "config",
    "sdkMembers",
    "sdkExpansions",
    "state",
    "contextNotes",
    "skillSources",
    "metrics",
    "inspectWidgets",
    "providers",
    "decisionProviders",
]


# meiva:link base/packages/plugins/docs/runtime-contributions.meiva.okf.md
@dataclass(frozen=True)
class Contribution:
    surface: SurfaceId
    identifier: str
    plugin: str
    detail: str
