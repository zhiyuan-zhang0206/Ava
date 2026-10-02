"""Compatibility facade for the live Loki event read.

Only the backfill scripts read Loki (`query_events`); every gateway reader uses `telemetry_events`.
The private siblings own transport, LogQL construction and event rows.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import ClassVar

from gateway.lgtm import _loki_event_rows, _loki_logql, _loki_transport

# Compatibility test seams retained while their owners live in private modules.
telemetry = _loki_transport.telemetry
settings = _loki_event_rows.settings

ObservabilityReadUnavailable = _loki_transport.ObservabilityReadUnavailable

_read_gate = _loki_transport._read_gate
_log_loki_failure = _loki_transport._log_loki_failure
_get_json = _loki_transport._get_json
_client = _loki_transport._client

_escape_label = _loki_logql._escape_label
_tier_event_names = _loki_logql._tier_event_names
_event_name_regex = _loki_logql._event_name_regex
_tier_predicate = _loki_logql._tier_predicate
_build_logql = _loki_logql._build_logql
_window = _loki_logql._window

_parse_line = _loki_event_rows._parse_line
query_events = _loki_event_rows.query_events


class _LokiEventsFacade(ModuleType):
    """Forward legacy monkeypatch seams to their focused implementation owner."""

    _SEAM_TARGETS: ClassVar[dict[str, tuple[ModuleType, str]]] = {
        "_client": (_loki_transport, "_client"),
        "_get_json": (_loki_transport, "_get_json"),
        "_log_loki_failure": (_loki_transport, "_log_loki_failure"),
        "_read_gate": (_loki_transport, "_read_gate"),
    }

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        target = self._SEAM_TARGETS.get(name)
        if target is not None:
            module, attribute = target
            setattr(module, attribute, value)


sys.modules[__name__].__class__ = _LokiEventsFacade
