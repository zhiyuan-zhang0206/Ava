from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ava:ref base/packages/plugins/docs/inspector-widgets.ava.okf.md#widget-declaration
WIDGET_KINDS = ("taskList",)

WidgetKind = Literal["taskList"]


class PluginInspectorError(Exception):
    pass


# ava:ref base/packages/plugins/docs/inspector-widgets.ava.okf.md#widget-registration
class DuplicateInspectWidget(PluginInspectorError):
    pass


# ava:ref base/packages/plugins/docs/inspector-widgets.ava.okf.md#widget-order-and-title
class InspectWidgetSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plugin: str = ""
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    kind: WidgetKind
    order: int
    title: str | None = Field(default=None, min_length=1)
