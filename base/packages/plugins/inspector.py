"""Plugin inspector widgets — plugins embed per-agent widgets in the Inspector Panel.

The declaration half of the inspector-widget surface (design: task #2909).
A plugin declares what the panel shows for **every agent** from its own Python
half: its ``inspector.py`` exports ``contribute()``, a pure function returning a
``PluginContributions`` with ``inspect_widgets``, exactly like a plugin's
``metrics.py``. The gateway loads each enabled plugin's ``inspector.py`` into a
data registry (``base/packages/plugins/data_registry.py``,
``gateway/inspect/_plugin_widgets.py``) and serves the resolved widgets per
agent from ``GET /api/agents/{id}/inspect/widgets``.

**The console never executes plugin code or markup.** A widget is closed-set
data rendered by the console's own components: a ``kind`` from
``WIDGET_KINDS``. Anything unknown — a kind from a newer kernel, a drifted
field — is skipped at render time or rejected here at validation; it is
never interpreted.

**Declaration is data; resolution is kernel-side.** The spec carries
no callables and no queries. Which rows a widget resolves for an agent is the
gateway's job (a ``taskList`` lists the agent's active tasks — complete, no
cap). A widget with no data renders nothing — it shrinks, it does not
lie.

The data registry validates the declaration (a duplicate id within a plugin is refused) and
fills each spec's ``plugin`` from the registry entry; nothing is registered process-wide.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The closed kind set — the taskList family (task #3216, reshaped from the
# #2909 jump buttons). A new family is a deliberate change here plus a
# renderer branch in `ui/web/src/components/inspector-widgets.tsx`, not a
# free-form field.
WIDGET_KINDS = ("taskList",)

WidgetKind = Literal["taskList"]


class PluginInspectorError(Exception):
    """Base class for inspector-widget registration errors."""


class DuplicateInspectWidget(PluginInspectorError):  # noqa: N818 — parallel to DuplicateMetric
    """A plugin declares two widgets with the same id."""


class InspectWidgetSpec(BaseModel):
    """One widget a plugin embeds in the agent-inspect panel.

    ``order`` positions the widget among the panel's sections: the built-in
    sections carry documented order keys (100 page / 200 shells / 300 liveness
    / 400 config overlay / 500 cost / 600 activity / 700 run-timeline link /
    800 notice — see `docs/conventions/plugin-spec-v2.md`), any int slots between
    them, and equal orders stack kernel-first, then by (plugin, id). ``title``
    is an optional section header; without it the widget renders headerless —
    except kinds the console titles by default (``taskList``: "Tasks"),
    which use their own localized copy when ``title`` is unset.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Filled with the declaring plugin's name when the data registry admits the
    # declaration, overriding the author's value (mirrors MetricSpec.plugin).
    plugin: str = ""
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    kind: WidgetKind
    order: int
    title: str | None = Field(default=None, min_length=1)
