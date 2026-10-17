from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# meiva:link base/packages/plugins/docs/inspector-widgets.meiva.okf.md
WIDGET_KINDS = ("taskList",)

WidgetKind = Literal["taskList"]


class PluginInspectorError(Exception):
    pass


# meiva:note
# The data registry admits a face whole: a duplicate widget id within one plugin refuses that plugin (DuplicateInspectWidget), the same id across plugins is admitted, and each spec's plugin is filled from the registry entry, overriding an author-supplied value. Nothing is registered process-wide.
# meiva:link base/packages/plugins/docs/inspector-widgets.meiva.okf.md
class DuplicateInspectWidget(PluginInspectorError):
    pass


# meiva:note
# order is any int; the panel's built-in section keys leave room to slot a widget anywhere, and equal orders stack the kernel's sections first, then widgets by (plugin, id). title is an optional section header; without it the widget renders headerless, except kinds the console titles by default (taskList uses the console's own localized "Tasks").
# meiva:link base/packages/plugins/docs/inspector-widgets.meiva.okf.md
class InspectWidgetSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plugin: str = ""
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    kind: WidgetKind
    order: int
    title: str | None = Field(default=None, min_length=1)
