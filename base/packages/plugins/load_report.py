"""The loud half of the plugin fail-soft contract — one reporter for every load site.

A plugin that fails to load is *contained*: skipped so it can never drag down
the process around it (user ruling 2026-09-11, after the 2026-08-28 ava_ledger
and 2026-09-10 agent-host incidents), and this module makes the skip VISIBLE —
a loguru ERROR carrying the traceback plus one `plugin_load_failed` telemetry
event (anomaly tier), so ops sees which plugin broke and why wherever it broke.

Call it through the module attribute (`plugin_load_report.report_plugin_load_failure`),
never a ``from ... import report_plugin_load_failure`` alias: a test substitutes
this function, and only an attribute-level call observes the substitution.
"""

from __future__ import annotations

from collections.abc import Callable

Reporter = Callable[[str, BaseException], None]


def reporter(report: Reporter | None) -> Reporter:
    """The reporter a load site calls: the caller's own (a read-only check that wants the failures
    as values, e.g. `ava plugins verify`), else the canonical one below, looked up at call time."""
    return report if report is not None else report_plugin_load_failure


def report_plugin_load_failure(name: str, exc: BaseException) -> None:
    """Report one plugin that could not be loaded and was skipped.

    Never raises by itself — the failure already happened, and containment is
    the point.
    """
    from base.log import logger
    from base.telemetry import emit

    logger.opt(exception=exc).error(
        "[plugins] plugin {} failed to load — skipped (fail-soft); "
        "the remaining plugins still load",
        name,
    )
    try:
        emit(
            "telemetry",
            "plugin_load_failed",
            level="error",
            attributes={"plugin": name, "error": f"{type(exc).__name__}: {exc}"},
        )
    except Exception:
        # `_no_emitter`: the failure may be the telemetry pipeline itself; its report must not
        # re-enter it. The ERROR above already carries the load failure.
        logger.bind(_no_emitter=True).opt(exception=True).warning(
            "[plugins] plugin_load_failed event for {} was not emitted; only the log line records it",
            name,
        )
