"""LogQL template validation for metric registries (task #1280).

The metric registry (``shared/plugin_metrics.py``) validates its SQL
templates against a whitelist; the migrated core panels (Task #1280) query
the event stream in Loki instead, and their LogQL templates are validated
here instead — a lightweight contract rather than a grammar whitelist:

- every template must select the event stream
  (``{service_name="unknown_service"}`` — the unified emitter's OTLP
  resource, see gateway/loki_events.py) and pipeline ``| json``:
  ``event_name`` (and ``agent_id``) became index labels at the 2026-08-23
  cutover (shared/loki_index_labels.py, task #1467), so event-scoped
  templates match them INSIDE the stream selector
  (``{service_name="unknown_service", event_name=...}``); the level /
  category / attributes.* fields are NOT stream labels, so their filters
  stay after the ``| json`` stage (before it they would match nothing).
  The base ``service_name`` selector and the ``| json`` stage are
  mandatory either way;
- event_name/category filters must go through the {event_name}/{category}
  placeholders (registry metadata is the single source of truth) unless the
  query filters on neither (whole-stream queries are legitimate).

``validate_logql`` re-validates a RENDERED query (placeholders substituted)
— the inspector's defense against a tampered registry file.

Imports stay one-directional (metrics_logql -> plugin_metrics): the
exception class lives in the registry module and is referenced through the
module object at call time, so plugin_metrics may import this module
without a cycle.
"""

from __future__ import annotations

import shared.plugin_metrics as _plugin_metrics

_LOKI_EVENT_SELECTOR = 'service_name="unknown_service"'
"""The event-stream selector every LogQL template must start with — the OTLP
resource the unified emitter ships to (gateway/loki_events.py: _SELECTOR)."""


def _validate_logql_template(template: str, name: str) -> None:
    """Lightweight LogQL template checks (task #1280): the query must select
    the event stream and pipeline ``| json``. event_name/agent_id are promoted
    index labels since the 2026-08-23 cutover, so event-scoped templates match
    them inside the stream selector; the level/category/attributes filters
    stay after ``| json`` (those fields are not stream labels). The base
    ``service_name`` selector and the ``| json`` stage are mandatory either
    way. Event_name/category filters must go through the
    {event_name}/{category} placeholders (registry metadata is the single
    source of truth) unless the query has no event_name/category filter at
    all — whole-stream queries (e.g. the event rate panel) legitimately
    filter on neither."""
    if _LOKI_EVENT_SELECTOR not in template:
        raise _plugin_metrics.InvalidMetricQuery(
            f"metric {name!r} LogQL query must select the event stream {{{_LOKI_EVENT_SELECTOR}}}"
        )
    if "| json" not in template:
        raise _plugin_metrics.InvalidMetricQuery(
            f"metric {name!r} LogQL query must pipeline | json before any "
            "event-field filter (event fields are structured metadata, not "
            "stream labels)"
        )
    has_placeholder = (
        "{event_name}" in template or "{category}" in template or "{category_re}" in template
    )
    has_event_filter = "event_name=" in template or "category=" in template
    if not has_placeholder and has_event_filter:
        raise _plugin_metrics.InvalidMetricQuery(
            f"metric {name!r} LogQL query filters event_name/category without "
            "the {event_name}/{category} placeholders (registry metadata is "
            "the single source of truth)"
        )


def validate_logql(query: str, name: str) -> None:
    """Re-validate a RENDERED LogQL query (placeholders substituted) — the
    inspector's defense against a tampered registry file. The template-form
    placeholder check does not apply post-render (every placeholder is gone
    by construction); the stream selector and json pipeline must survive."""
    if _LOKI_EVENT_SELECTOR not in query:
        raise _plugin_metrics.InvalidMetricQuery(
            f"metric {name!r} rendered LogQL query lost the event stream "
            f"selector {{{_LOKI_EVENT_SELECTOR}}}"
        )
    if "| json" not in query:
        raise _plugin_metrics.InvalidMetricQuery(
            f"metric {name!r} rendered LogQL query lost the | json pipeline"
        )


def validate_spec_logql(spec: _plugin_metrics.MetricSpec) -> None:
    """Validate every template of a logql MetricSpec (query + targets) —
    the dialect branch of ``plugin_metrics.validate_spec_sql``, kept here so
    this module owns the whole LogQL contract."""
    for template in [spec.query, *(spec.targets or [])]:
        _validate_logql_template(template, spec.name)
