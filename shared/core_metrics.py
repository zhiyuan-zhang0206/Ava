"""Core metrics — the first-party observability surface (Task #882).

Two-tier metric architecture since 2026-08-06 (user ruling):

- **Core metrics** (this module): the repo's own observability — LLM cost /
  errors / turn health / exec outcomes / SDK usage, plus the hand-written
  ops-dashboard panels migrated into registry form. Registered with
  ``register_core_metric``, which runs the SAME template safety validation
  as plugin metrics but never requires a PluginContext: core metrics are
  repo code, not plugin code.
- **Plugin metrics** (``shared/plugin_metrics.py``): metrics contributed by
  first-party business plugins (ava_code / ava_fleet / ava_memory) or
  external plugins, registered under their plugin name.

The dashboards (``deploy/lgtm/config/grafana/provisioning/dashboards/
ava-ops-main.json``) carry the **core section first** — row header ``core``,
then one row per plugin. The inspector surface (``gateway/routers/
_plugin_metrics.py``) builds both registries in process — task #180 PR D
replaced the generator's state snapshot ($AVA_HOME/state/plugin_metrics.json),
which froze when the generator did not survive the archive->public port.

Core definitions live in ``shared/core_metrics_panels.py`` (the migrated
ops-dashboard panels) and ``shared/core_metrics_observability.py`` (the
migrated ava_observability pack). The ``plugin`` field of every core metric
is ``core`` — the dashboard row header and the display name are "core".
"""

from __future__ import annotations

import importlib
from contextlib import suppress

from shared.plugin_metrics import (
    DuplicateMetric,
    MetricSpec,
    validate_spec_sql,
)

# Definition modules the generator imports (in registration order across
# modules: panels first, then observability — the dashboard's core section
# follows that order). A missing module is tolerated (a partial checkout /
# test env without the definitions) and renders an empty core section.
_CORE_DEFINITION_MODULES = (
    "shared.core_metrics_panels",
    "shared.core_metrics_dismissed",
    "shared.core_metrics_fleet",
    "shared.core_metrics_observability",
)

_CORE_REGISTRY: dict[str, MetricSpec] = {}


def register_core_metric(spec: MetricSpec) -> MetricSpec:
    """Register one core metric (first-party observability surface).

    Validation at register time: name uniqueness across core metrics and
    query safety for every template (``validate_spec_sql`` — the same checks
    plugin metrics go through).

    Raises:
        DuplicateMetric: ``spec.name`` already registered.
        InvalidMetricQuery: any template failed validation.
    """
    if spec.name in _CORE_REGISTRY:
        raise DuplicateMetric(
            f"core metric {spec.name!r} already registered — names are global across core metrics."
        )
    validate_spec_sql(spec)
    filled = spec.model_copy(update={"plugin": "core"})
    _CORE_REGISTRY[spec.name] = filled
    return filled


def registered_core_metrics() -> list[MetricSpec]:
    """All registered core metrics, in registration order."""
    return list(_CORE_REGISTRY.values())


def clear_core_registry() -> None:
    """Drop every registration — test fixtures."""
    _CORE_REGISTRY.clear()


def collect_core_metrics() -> list[MetricSpec]:
    """Import the core definition modules (once) and return their metrics in
    registration order. A module that cannot be imported (missing dependency
    or not present) is skipped — same tolerance as the plugin generator."""
    for module_name in _CORE_DEFINITION_MODULES:
        with suppress(ImportError):
            importlib.import_module(module_name)
    return registered_core_metrics()
