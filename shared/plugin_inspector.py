"""Plugin inspector widgets — plugins embed per-agent widgets in the Inspector Panel.

The registration half of the inspector-widget surface (design: task #2909).
A plugin declares what the panel shows for **every agent** from its own Python
half, at import time, exactly like ``shared/plugin_metrics.py``: the gateway
imports each enabled plugin's ``inspector.py`` under its ``PluginContext``
(``gateway/routers/_plugin_inspector.py``) and serves the resolved widgets per
agent from ``GET /api/agents/{id}/inspect/widgets``.

**The console never executes plugin code or markup.** A widget is closed-set
data rendered by the console's own components: a ``kind`` from
``WIDGET_KINDS``. Anything unknown — a kind from a newer kernel, a drifted
field — is skipped at render time or rejected here at registration; it is
never interpreted.

**Registration is declarative; resolution is kernel-side.** The spec carries
no callables and no queries. Which rows a widget resolves for an agent is the
gateway's job (a ``taskList`` lists the agent's active tasks, capped
kernel-side). A widget with no data renders nothing — it shrinks, it does not
lie.

Registration mirrors the metric registry: call ``register_inspect_widget``
inside ``PluginContext`` (the framework wraps plugin imports), the plugin name
is auto-filled, duplicate ids within a plugin are refused, and the registry is
process-local (module caching makes repeated loads free; a plugin disabled
before a restart is filtered out by the loader's enabled-set check).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.plugin_context import current_plugin_name

# The closed kind set — the taskList family (task #3216, reshaped from the
# #2909 jump buttons). A new family is a deliberate change here plus a
# renderer branch in `ui/web/src/components/inspector-widgets.tsx`, not a
# free-form field.
WIDGET_KINDS = ("taskList",)

WidgetKind = Literal["taskList"]


class PluginInspectorError(Exception):
    """Base class for inspector-widget registration errors."""


class NoPluginContext(PluginInspectorError):  # noqa: N818 — parallel to plugin_metrics' NoPluginContext
    """Registration ran outside ``with PluginContext(...)``."""


class DuplicateInspectWidget(PluginInspectorError):  # noqa: N818 — parallel to DuplicateMetric
    """A widget with this id is already registered by this plugin."""


class InspectWidgetSpec(BaseModel):
    """One widget a plugin embeds in the agent-inspect panel.

    ``order`` positions the widget among the panel's sections: the built-in
    sections carry documented order keys (100 page / 200 shells / 300 liveness
    / 400 config overlay / 500 cost / 600 activity / 700 run-timeline link /
    800 notice — see `conventions/plugin-spec-v2.md`), any int slots between
    them, and equal orders stack kernel-first, then by (plugin, id). ``title``
    is an optional section header; without it the widget renders headerless —
    except kinds the console titles by default (``taskList``: "Today's tasks"),
    which use their own localized copy when ``title`` is unset.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Auto-filled from PluginContext at registration, overriding the author's
    # value (mirrors MetricSpec.plugin).
    plugin: str = ""
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    kind: WidgetKind
    order: int
    title: str | None = Field(default=None, min_length=1)


# ── registry ──────────────────────────────────────────────────────────────────

_REGISTRY: dict[tuple[str, str], InspectWidgetSpec] = {}


def register_inspect_widget(spec: InspectWidgetSpec) -> InspectWidgetSpec:
    """Register one widget — must run inside PluginContext (the framework
    wraps plugin imports; the loader wraps ``inspector.py`` imports with the
    plugin name).

    Validation: the spec model (closed kind/target/icon sets) plus id
    uniqueness within the plugin. The ``plugin`` field is auto-filled from the
    context, overriding whatever the author passed.

    Raises:
        NoPluginContext: called outside ``with PluginContext(...)``.
        DuplicateInspectWidget: ``(plugin, spec.id)`` already registered.
    """
    plugin = current_plugin_name()
    if plugin is None:
        raise NoPluginContext(
            "register_inspect_widget() must be called inside PluginContext — the "
            "framework `_load_extensions` wraps plugin imports; the gateway's "
            "inspector loader wraps `inspector.py` imports with the plugin name."
        )
    key = (plugin, spec.id)
    if key in _REGISTRY:
        raise DuplicateInspectWidget(f"widget {spec.id!r} already registered by plugin {plugin!r}")
    filled = spec.model_copy(update={"plugin": plugin})
    _REGISTRY[key] = filled
    return filled


def registered_inspect_widgets() -> list[InspectWidgetSpec]:
    """All registered widgets, in registration order."""
    return list(_REGISTRY.values())


def clear_registry() -> None:
    """Drop every registration — test fixtures."""
    _REGISTRY.clear()


def drop_plugin_inspect_widgets(plugin: str) -> list[str]:
    """Drop every widget registered by ``plugin``; return the dropped ids.

    The gateway's inspector loader (`gateway/routers/_plugin_inspector.py`)
    calls this after a failed ``inspector.py`` import: registration is not
    transactional, so without the cleanup a module that raised mid-way would
    leave its partial entries registered while every retry of the fixed file
    died on ``DuplicateInspectWidget`` — the plugin stuck on the failed path
    until process restart.
    """
    keys = [key for key in _REGISTRY if key[0] == plugin]
    for key in keys:
        del _REGISTRY[key]
    return [widget_id for _plugin, widget_id in keys]
