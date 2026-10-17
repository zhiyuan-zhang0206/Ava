from __future__ import annotations

from base.log import logger
from base.packages.plugins.contributions import SurfaceId
from base.telemetry import report_sink_failure

PLUGIN_ACTIVATION_EVENT = "plugin_activation"


# meiva:ref base/packages/plugins/docs/runtime-contributions.meiva.okf.md#activation-attribution
def record(
    plugin: str | None,
    surface: SurfaceId,
    identifier: str,
    *,
    detail: str = "",
    model: str,
) -> None:
    if plugin is None:
        return
    try:
        emit(plugin, surface, identifier, detail, model)
    except Exception as exc:
        report_sink_failure("plugin_activation recording (activation events are dropped)", exc)


def emit(plugin: str, surface: SurfaceId, identifier: str, detail: str, model: str) -> None:

    logger.bind(
        event=PLUGIN_ACTIVATION_EVENT,
        plugin=plugin,
        surface=surface,
        identifier=identifier,
        detail=detail,
        model=model,
    ).info("plugin_activation")
