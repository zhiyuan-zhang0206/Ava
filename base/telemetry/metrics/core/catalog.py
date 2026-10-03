"""Core metrics — the first-party observability surface (Task #882).

Two-tier metric architecture since 2026-08-06 (user ruling):

- **Core metrics** (this module): the repo's own observability — LLM cost /
  errors / turn health / exec outcomes / SDK usage, plus the hand-written
  ops-dashboard panels migrated into registry form. Each definition module returns its
  specs from ``core_metrics()`` and ``collect_core_metrics`` runs the SAME template safety validation
  as plugin metrics but is not admitted through a plugin entry: core metrics are
  repo code, not plugin code.
- **Plugin metrics** (``base/telemetry/metrics/plugin_metrics.py``): metrics contributed by
  first-party business plugins (ava_fleet / ava_memory / ava_syntax_fix) or
  external plugins, declared under their plugin name (`PluginContributions.metrics`).

The dashboards (``deploy/lgtm/config/grafana/provisioning/dashboards/
ava-ops-main.json``) carry the **core section first** — row header ``core``,
then one row per plugin. The inspector surface (``gateway/routers/
_plugin_metrics.py``) builds both registries in process — task #180 PR D
replaced the generator's state snapshot ($AVA_HOME/state/plugin_metrics.json),
which froze when the generator did not survive the archive->public port. The
dashboard render is being rebuilt (task #3697): ``base.telemetry.metrics.grafana_dashboard``
renders the registries into the dashboard JSON, previewed by ``ava lgtm
render`` and wired into converge by slice S3.

Core definitions live in ``base/telemetry/metrics/core/panels.py`` (the migrated
ops-dashboard panels), ``base/telemetry/metrics/core/observability.py`` (the migrated
ava_observability pack), ``base/telemetry/metrics/core/events.py`` (the event-stream
panels: the Events trio and the gateway sample count) and
``base/telemetry/metrics/core/host.py`` (the "Host & data plane" section), plus the
smaller modules beside them (``cost`` / ``frontend`` / ``dismissed`` / ``fleet``
/ ``pr_flow``).
The ``plugin`` field of
every core metric is ``core`` — the dashboard row header and the display name
are "core".
"""

from __future__ import annotations

import importlib
from typing import Any, cast

from base.telemetry.metrics.plugin_metrics import (
    DuplicateMetric,
    MetricSpec,
    validate_spec_sql,
)

# Definition modules collected (in registration order across modules). The
# renderer orders the dashboard by each spec's ``section`` + ``order`` pins,
# not this sequence — it only breaks ties for un-pinned specs and shapes the
# inspector listing. A missing module is tolerated (a partial checkout / test
# env without the definitions) and renders an empty core section.
_CORE_DEFINITION_MODULES = (
    "base.telemetry.metrics.core.panels",
    "base.telemetry.metrics.core.cost",
    "base.telemetry.metrics.core.dismissed",
    "base.telemetry.metrics.core.fleet",
    "base.telemetry.metrics.core.pr_flow",
    "base.telemetry.metrics.core.ci",
    "base.telemetry.metrics.core.events",
    "base.telemetry.metrics.core.host",
    "base.telemetry.metrics.core.observability",
    "base.telemetry.metrics.core.exec_envelope",
    "base.telemetry.metrics.core.frontend",
)


def validate_core_metric(spec: MetricSpec) -> MetricSpec:
    """One core metric (first-party observability surface) as the registry holds it.

    Query safety for every template (``validate_spec_sql`` — the same checks
    plugin metrics go through); the ``plugin`` field is ``core``.

    Raises:
        InvalidMetricQuery: any template failed validation.
    """
    validate_spec_sql(spec)
    return spec.model_copy(update={"plugin": "core"})


def collect_core_metrics() -> list[MetricSpec]:
    """The core metrics of every definition module, validated, in registration order.

    A module that cannot be imported (missing dependency or not present) is
    skipped — same tolerance as the plugin generator.

    Raises:
        DuplicateMetric: two definitions share a name (names are global across core metrics).
        InvalidMetricQuery: any template failed validation.
    """
    collected: dict[str, MetricSpec] = {}
    for module_name in _CORE_DEFINITION_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for spec in cast("Any", module).core_metrics():
            if spec.name in collected:
                raise DuplicateMetric(
                    f"core metric {spec.name!r} already registered — names are global across core metrics."
                )
            collected[spec.name] = validate_core_metric(spec)
    return list(collected.values())
