"""LogQL template validation for metric registries (task #1280).

The metric registry (``base/telemetry/metrics/plugin_metrics.py``) validates its SQL
templates against a whitelist; the migrated core panels (Task #1280) query
the event stream in Loki instead, and their LogQL templates are validated
here instead — a lightweight contract rather than a grammar whitelist:

- every template must select the event stream
  (``{service_name="unknown_service"}`` — the unified emitter's OTLP
  resource, ``base/loki_index_labels.EVENT_STREAM_SERVICE_NAME``) and
  pipeline ``| json``:
  ``event_name`` (and ``agent_id``) became index labels at the 2026-08-23
  cutover (base/telemetry/loki_index_labels.py, task #1467), so event-scoped
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

It also owns the template-building vocabulary every metric definition
(core panels and plugin ``metrics.py`` alike) writes its queries in:
``EVENT_SELECTOR`` / ``EVENT_NAME_SELECTOR``, ``event_count``,
``CATEGORY_MATCH`` and ``CATEGORY_OR_LOG``.

Imports stay one-directional: this module raises the registry's `InvalidMetricQuery` (from
`plugin_metrics_sql`) and never imports `plugin_metrics`, which calls
`validate_logql_template` for logql specs.
"""

from __future__ import annotations

from base.telemetry.loki_index_labels import EVENT_STREAM_SERVICE_NAME
from base.telemetry.metrics.plugin_metrics_sql import InvalidMetricQuery

EVENT_STREAM_MATCHER = f'service_name="{EVENT_STREAM_SERVICE_NAME}"'
"""The event-stream matcher every LogQL template must select on — the OTLP
resource the unified emitter ships to."""

EVENT_SELECTOR = f"{{{EVENT_STREAM_MATCHER}}}"
"""The whole event stream; the level/category/attributes filters follow
``| json`` (they are not stream labels)."""

EVENT_NAME_SELECTOR = f"{{{EVENT_STREAM_MATCHER}, event_name={{event_name}}}}"
"""The stream of one registered event: event_name is a promoted index label
(2026-08-23 cutover), so it is matched inside the selector through the
``{event_name}`` placeholder."""

CATEGORY_MATCH = 'category=~"{category_re}"'
"""Category filter for a metric scoped to one registered event: ``{category_re}``
renders the event's declared category unquoted for the regex."""

CATEGORY_OR_LOG = 'category=~"{category_re}|log"'
"""Category filter for whole-stream health panels that count the declared
category together with the ``log`` category (bare logger lines and the
log-category warnings)."""


def event_count(pipeline: str, window: str, matchers: str | None = None) -> str:
    """One count_over_time series — every count wraps in sum(...): the
    unknown_service family has >500 streams over a day, and an unaggregated
    count_over_time hits Loki's per-query series cap (alert-rules note).

    ``matchers`` carries the promoted event_name/agent_id stream-label matcher
    (e.g. ``'event_name={event_name}'`` or ``'event_name=~"a|b"'``): indexed-era
    reads match those labels inside the stream selector
    (base/telemetry/loki_index_labels.py), not after ``| json``."""
    selector = EVENT_SELECTOR if matchers is None else f"{{{EVENT_STREAM_MATCHER}, {matchers}}}"
    return f"sum(count_over_time({selector} | json | {pipeline} [{window}]))"


def validate_logql_template(template: str, name: str, *, raw_view: bool = False) -> None:
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
    filter on neither. ``raw_view`` (a ``logs`` panel) waives the placeholder
    rule: a raw stream view defines its own predicate — the Events tier view
    filters level/category/event_name as panel content, not as registry
    metadata — so only the stream-selector and json checks apply."""
    if EVENT_STREAM_MATCHER not in template:
        raise InvalidMetricQuery(
            f"metric {name!r} LogQL query must select the event stream {{{EVENT_STREAM_MATCHER}}}"
        )
    if "| json" not in template:
        raise InvalidMetricQuery(
            f"metric {name!r} LogQL query must pipeline | json before any "
            "event-field filter (event fields are structured metadata, not "
            "stream labels)"
        )
    has_placeholder = (
        "{event_name}" in template or "{category}" in template or "{category_re}" in template
    )
    has_event_filter = "event_name=" in template or "category=" in template
    if not has_placeholder and has_event_filter and not raw_view:
        raise InvalidMetricQuery(
            f"metric {name!r} LogQL query filters event_name/category without "
            "the {event_name}/{category} placeholders (registry metadata is "
            "the single source of truth)"
        )


def validate_logql(query: str, name: str) -> None:
    """Re-validate a RENDERED LogQL query (placeholders substituted) — the
    inspector's defense against a tampered registry file. The template-form
    placeholder check does not apply post-render (every placeholder is gone
    by construction); the stream selector and json pipeline must survive."""
    if EVENT_STREAM_MATCHER not in query:
        raise InvalidMetricQuery(
            f"metric {name!r} rendered LogQL query lost the event stream "
            f"selector {{{EVENT_STREAM_MATCHER}}}"
        )
    if "| json" not in query:
        raise InvalidMetricQuery(f"metric {name!r} rendered LogQL query lost the | json pipeline")
