"""The loud half of the plugin fail-soft contract — one reporter for every load site.

A plugin that fails to load is *contained*: skipped so it can never drag down
the process around it (user ruling 2026-09-11, after the 2026-08-28 ava_ledger
and 2026-09-10 agent-host incidents), and this module makes the skip VISIBLE —
a loguru ERROR carrying the traceback plus one `plugin_load_failed` telemetry
event (anomaly tier), so ops sees which plugin broke and why wherever it broke.

Call it through the module attribute (`plugin_load_report.report_plugin_load_failure`),
never a ``from ... import report_plugin_load_failure`` alias: the release probe
(`cli/commands/_release_plugin_probe.py`) substitutes this function to turn the
containment back into a hard release rejection, and only an attribute-level
call observes the substitution.
"""

from __future__ import annotations

import contextlib


def report_plugin_load_failure(name: str, exc: BaseException) -> None:
    """Report one plugin that could not be loaded and was skipped.

    Never raises by itself — the failure already happened, and containment is
    the point. The one substitution is deliberate: the release probe replaces
    this function with a raising one so a candidate image with an unloadable
    plugin is rejected instead of degraded.
    """
    from shared.log import logger
    from shared.telemetry import emit

    logger.error(
        "[plugins] plugin {} failed to load — skipped (fail-soft); "
        "the remaining plugins still load",
        name,
        exc_info=exc,
    )
    with contextlib.suppress(Exception):
        emit(
            "telemetry",
            "plugin_load_failed",
            level="error",
            attributes={"plugin": name, "error": f"{type(exc).__name__}: {exc}"},
        )
