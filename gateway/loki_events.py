"""Compatibility facade for Loki-backed event-history reads.

The public gateway.loki_events module remains stable while focused private
siblings own transport, LogQL construction, event rows, and aggregates.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import ClassVar

from gateway import _loki_aggregates, _loki_event_rows, _loki_logql, _loki_transport

# Compatibility test seams retained while their owners live in private modules.
telemetry = _loki_transport.telemetry
settings = _loki_event_rows.settings

ObservabilityReadUnavailable = _loki_transport.ObservabilityReadUnavailable

_read_gate = _loki_transport._read_gate
_log_loki_failure = _loki_transport._log_loki_failure
_get_json = _loki_transport._get_json
_result_value = _loki_transport._result_value
_client = _loki_transport._client

_escape_label = _loki_logql._escape_label
_tier_event_names = _loki_logql._tier_event_names
_event_name_regex = _loki_logql._event_name_regex
_tier_predicate = _loki_logql._tier_predicate
_build_logql = _loki_logql._build_logql
_window = _loki_logql._window
_read_slices = _loki_logql._read_slices
_slice_duration_s = _loki_logql._slice_duration_s
_agg_pipeline = _loki_logql._agg_pipeline
_agg_pipelines = _loki_logql._agg_pipelines
_range_eras = _loki_logql._range_eras
_weighted_quantile = _loki_logql._weighted_quantile

_parse_line = _loki_event_rows._parse_line
query_events = _loki_event_rows.query_events
count_events = _loki_event_rows.count_events
metric_range = _loki_event_rows.metric_range
query_projected_lines = _loki_event_rows.query_projected_lines

attribute_aggregate = _loki_aggregates.attribute_aggregate
count_by_event_name = _loki_aggregates.count_by_event_name
attribute_distribution = _loki_aggregates.attribute_distribution
count_grouped = _loki_aggregates.count_grouped
count_event_classes = _loki_aggregates.count_event_classes
count_events_series = _loki_aggregates.count_events_series
attribute_max_series = _loki_aggregates.attribute_max_series


class _LokiEventsFacade(ModuleType):
    """Forward legacy monkeypatch seams to their focused implementation owner."""

    _SEAM_TARGETS: ClassVar[dict[str, tuple[ModuleType, str]]] = {
        "_client": (_loki_transport, "_client"),
        "_get_json": (_loki_transport, "_get_json"),
        "_log_loki_failure": (_loki_transport, "_log_loki_failure"),
        "_read_gate": (_loki_transport, "_read_gate"),
        "_read_slices": (_loki_logql, "_read_slices"),
    }

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        target = self._SEAM_TARGETS.get(name)
        if target is not None:
            module, attribute = target
            setattr(module, attribute, value)


sys.modules[__name__].__class__ = _LokiEventsFacade
