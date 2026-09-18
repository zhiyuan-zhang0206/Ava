"""Grafana dashboard rendering — the metric registry into the ava-ops-main JSON.

Task #3697 slice S1 (parent #3689): the single shipped dashboard
(``deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json``)
becomes a render of the metric registries — every panel comes from a
registered ``MetricSpec`` (core definitions in ``shared/core_metrics_*``, plugin
definitions in each plugin's ``metrics.py``), and slice S3 writes the render
into the station's provisioning directory.

``render_dashboard`` is a **pure function** of (core_specs, plugin_specs) plus
the frozen constants below: no environment, clock, or machine state, so the
same spec set renders byte-identical JSON everywhere (the #3339/#2456
env-injection lesson, 1818 review note on task #3697).

Layout is a greedy 24-column flow: panels place left to right and wrap to the
next band when the panel would end past column 24; a section row header is a
24x1 row. The exceptions to the flow are all explicit data:

- ``order`` — a panel's render rank within its section (the curated board
  order; panels without one render after the ordered ones, in registration
  order).
- ``position`` — an absolute grid position for the rare panel whose historical
  placement deviates from the flow (the one pin: exec outcomes at 12,120).
- ``gap_before`` in the section registry — the historical three-row gap before
  the LLM row.

Panel ids: core panels carry their as-is id (``panel_id``), row ids come from
the section registry, and plugin ids are allocated from ``_PLUGIN_ID_BASE``
(1001) per sorted plugin block without gaps.

The plugin side of the spec set comes from ``shared.grafana_dashboard_supply``
(``load_repo_plugin_specs`` / ``load_installed_plugin_specs`` /
``collect_plugin_specs``) — kept out of this module so the renderer stays a
pure function with no import machinery or database access.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from shared.plugin_metrics import MetricSpec, render_targets

# ── shell constants ───────────────────────────────────────────────────────────
# The dashboard's fixed identity — uid, timezone, refresh/range defaults, and
# the tag set are user-visible anchors (bookmarks and the insights embed
# depend on the uid); never change them without a user ruling.

_COLUMNS = 24
_DASHBOARD_UID = "ava-ops-main"
_DEFAULT_RANGE = {"from": "now-24h", "to": "now"}

_LOKI = {"type": "loki", "uid": "loki"}
_PROMETHEUS = {"type": "prometheus", "uid": "prometheus"}
_POSTGRES = {"type": "postgres", "uid": "ops"}
_DATASOURCE = {"logql": _LOKI, "promql": _PROMETHEUS, "sql": _POSTGRES}


class DashboardRenderError(Exception):
    """A spec set that cannot render a coherent dashboard (missing section,
    id, or target names) — the caller refuses to write a half-dashboard."""


# ── section registry ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Section:
    """One dashboard section: its row header title, the row-header id, and the
    historical gap (empty rows) that precedes it."""

    title: str
    row_id: int
    gap_before: int = 0


_CORE_SECTIONS_PREFIX = (
    _Section("core", 900),
    # The three empty rows before LLM are the as-is file's geometry (core block
    # ends at row 66; the LLM row sits at 69) — historical, not a layout rule.
    _Section("LLM", 2002, gap_before=3),
    _Section("Gateway & execution", 2003),
    _Section("Fleet", 2004),
)
_CORE_SECTIONS_SUFFIX = (
    _Section("Host & data plane", 2006),
    _Section("Cost analysis", 2007),
    _Section("PR flow", 2008),
)
# Plugin sections — one row per metric-shipping plugin, sorted by plugin name,
# ids allocated from the 1000 block — render between the prefix and suffix
# groups (the merged dashboard's as-is order).
_PLUGIN_ID_BASE = 1001
_CORE_SECTION_TITLES = frozenset(
    section.title for section in _CORE_SECTIONS_PREFIX + _CORE_SECTIONS_SUFFIX
)


# ── panel look profiles ───────────────────────────────────────────────────────
# The as-is per-type/per-dialect look, mined from the hand-maintained file
# (task #3697). Spec-level ``options`` / ``custom`` / ``field_defaults`` merge
# on top of these; keys not present there keep the profile values.

_LOKI_TS_CUSTOM: dict[str, Any] = {
    "lineInterpolation": "smooth",
    "showPoints": "never",
    "fillOpacity": 12,
    "drawStyle": "line",
    "lineWidth": 1,
    "spanNulls": True,
}
_PROM_TS_CUSTOM: dict[str, Any] = {
    "drawStyle": "line",
    "lineWidth": 1,
    "fillOpacity": 8,
    "showPoints": "never",
}
_BARCHART_CUSTOM: dict[str, Any] = {
    "stacking": {"mode": "normal", "group": "A"},
    "fillOpacity": 85,
    "lineWidth": 1,
    "drawStyle": "bars",
    "barAlignment": 0,
}
_TABLE_CUSTOM: dict[str, Any] = {
    "align": "auto",
    "cellOptions": {"type": "auto"},
    "filterable": False,
    "inspect": False,
}

_STAT_OPTIONS: dict[str, Any] = {
    "colorMode": "value",
    "graphMode": "area",
    "justifyMode": "auto",
    "orientation": "auto",
    "reduceOptions": {"calcs": ["last"], "fields": "", "values": False},
    "textMode": "auto",
}
_LOKI_TS_OPTIONS: dict[str, Any] = {
    "legend": {
        "calcs": ["mean", "max", "last"],
        "displayMode": "table",
        "placement": "bottom",
        "showLegend": True,
    },
    "tooltip": {"mode": "multi", "sort": "desc"},
}
_PROM_TS_OPTIONS: dict[str, Any] = {
    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
    "tooltip": {"mode": "multi", "sort": "desc"},
}
_BARCHART_OPTIONS: dict[str, Any] = {
    "legend": {
        "calcs": ["sum", "max"],
        "displayMode": "table",
        "placement": "bottom",
        "showLegend": True,
    },
    "tooltip": {"mode": "multi", "sort": "desc"},
    "orientation": "auto",
    "xTickLabelRotation": 0,
    "xField": "Time",
}
_TABLE_OPTIONS: dict[str, Any] = {
    "cellHeight": "sm",
    "footer": {"countRows": False, "enablePagination": True, "show": False},
    "showHeader": True,
    "showTypeIcons": False,
    "sortBy": [],
}
_LOGS_OPTIONS: dict[str, Any] = {"showTime": True, "sortOrder": "Descending"}

_DEFAULT_SIZE = {"stat": (8, 4)}


def _merge(base: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Recursive merge of a look override onto a profile: dict values merge
    key-wise, everything else replaces. Returns a fresh dict."""
    merged: dict[str, Any] = dict(base)
    if not overrides:
        return merged
    copied: dict[str, Any] = copy.deepcopy(overrides)
    for key, value in copied.items():
        existing = merged.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            merged[key] = _merge(cast("dict[str, Any]", existing), cast("dict[str, Any]", value))
        else:
            merged[key] = value
    return merged


def _thresholds(spec: MetricSpec) -> dict[str, Any]:
    """``fieldConfig.defaults.thresholds``: the green base plus the spec's
    steps; an explicit empty list renders the empty steps list (the panel has
    no thresholds at all)."""
    if spec.thresholds is None:
        steps: list[dict[str, Any]] = [{"color": "green", "value": None}]
    elif not spec.thresholds:
        steps = []
    else:
        steps = [{"color": "green", "value": None}] + [
            {"color": step.color, "value": step.value} for step in spec.thresholds
        ]
    return {"mode": "absolute", "steps": steps}


def _field_config_defaults(spec: MetricSpec) -> dict[str, Any] | None:
    """The panel's ``fieldConfig.defaults`` — None for panels that carry no
    field config at all (logs panels)."""
    defaults: dict[str, Any]
    if spec.panel == "stat":
        defaults = {
            "color": {"mode": "fixed", "fixedColor": "blue"},
            "thresholds": _thresholds(spec),
            "unit": spec.unit,
        }
        if spec.custom:
            defaults["custom"] = copy.deepcopy(spec.custom)
    elif spec.panel == "timeseries":
        profile = _PROM_TS_CUSTOM if spec.query_type == "promql" else _LOKI_TS_CUSTOM
        defaults = {
            "color": (
                {"mode": "fixed", "fixedColor": "blue"}
                if spec.query_type == "promql"
                else {"mode": "palette-classic"}
            ),
            "thresholds": _thresholds(spec),
            "unit": spec.unit,
            "custom": _merge(profile, spec.custom),
        }
    elif spec.panel == "barchart":
        defaults = {
            "color": {"mode": "palette-classic"},
            "thresholds": _thresholds(spec),
            "unit": spec.unit,
            "custom": _merge(_BARCHART_CUSTOM, spec.custom),
        }
    elif spec.panel == "table":
        if spec.thresholds == []:
            # A table without threshold rules keeps the minimal field config
            # (no color mode, no cell custom) — the as-is look of the
            # cost/PR-flow tables.
            defaults = {"thresholds": _thresholds(spec), "unit": spec.unit}
        else:
            defaults = {
                "color": {"mode": "thresholds"},
                "thresholds": _thresholds(spec),
                "unit": spec.unit,
                "custom": _merge(_TABLE_CUSTOM, spec.custom),
            }
    else:  # logs
        return None
    if spec.field_defaults:
        # Top-level override: each field_defaults key replaces the profile's
        # value wholesale (e.g. a color-mode switch drops the profile's color
        # fields rather than leaving them half-merged).
        defaults = {**defaults, **copy.deepcopy(spec.field_defaults)}
    return defaults


def _options(spec: MetricSpec) -> dict[str, Any]:
    if spec.panel == "stat":
        profile = _STAT_OPTIONS
    elif spec.panel == "timeseries":
        profile = _PROM_TS_OPTIONS if spec.query_type == "promql" else _LOKI_TS_OPTIONS
    elif spec.panel == "barchart":
        profile = _BARCHART_OPTIONS
    elif spec.panel == "table":
        profile = _TABLE_OPTIONS
    else:  # logs
        profile = _LOGS_OPTIONS
    return _merge(profile, spec.options)


def _is_instant(spec: MetricSpec, first_query: str) -> bool:
    """Instant vs range for Loki targets: stats and tables are window totals,
    and a ``$__range`` aggregate is a window total whatever the panel type;
    everything else is a rate series."""
    return spec.panel in ("stat", "table") or "$__range" in first_query


def _targets(spec: MetricSpec) -> list[dict[str, Any]]:
    rendered = render_targets(spec)
    names = spec.target_names
    if spec.query_type != "sql" and names is None:
        raise DashboardRenderError(
            f"metric {spec.name!r} renders {spec.query_type} targets but has no target_names"
        )
    targets: list[dict[str, Any]] = []
    for index, expr in enumerate(rendered):
        ref_id = chr(ord("A") + index)
        legend = None if names is None else names[index]
        if spec.query_type == "sql":
            targets.append(
                {
                    "datasource": dict(_POSTGRES),
                    "editorMode": "code",
                    "format": "table",
                    "rawSql": expr,
                    "refId": ref_id,
                    "sql": {"columns": [], "groupBy": [], "orderBy": []},
                }
            )
        elif spec.query_type == "promql":
            target: dict[str, Any] = {
                "datasource": dict(_PROMETHEUS),
                "editorMode": "code",
                "expr": expr,
                "refId": ref_id,
                "legendFormat": legend,
            }
            if spec.panel in ("stat", "table"):
                target["instant"] = True
                target["range"] = False
                if spec.panel == "table":
                    target["format"] = "table"
            else:
                target["queryType"] = "range"
            targets.append(target)
        else:
            targets.append(
                {
                    "datasource": dict(_LOKI),
                    "editorMode": "code",
                    "expr": expr,
                    "refId": ref_id,
                    "queryType": "instant" if _is_instant(spec, rendered[0]) else "range",
                    "legendFormat": legend,
                }
            )
    return targets


def _panel(spec: MetricSpec, panel_id: int, grid: dict[str, int]) -> dict[str, Any]:
    panel: dict[str, Any] = {}
    defaults = _field_config_defaults(spec)
    if defaults is not None:
        panel["fieldConfig"] = {"defaults": defaults}
    panel.update(
        {
            "datasource": dict(_DATASOURCE[spec.query_type]),
            "gridPos": grid,
            "id": panel_id,
            "options": _options(spec),
            "targets": _targets(spec),
            "title": spec.title,
            "type": spec.panel,
        }
    )
    if spec.description:
        panel["description"] = spec.description
    if spec.transformations:
        panel["transformations"] = copy.deepcopy(spec.transformations)
    return panel


# ── layout ────────────────────────────────────────────────────────────────────


class _Layout:
    """Greedy 24-column flow with the two explicit deviations (pin, gap)."""

    def __init__(self) -> None:
        self._x = 0
        self._y = 0
        self._band_h = 0

    def place(
        self, width: int, height: int, *, position: tuple[int, int] | None = None
    ) -> dict[str, int]:
        """Place one rectangle; returns its gridPos. A pinned panel lands at
        its absolute position and the flow continues from the row below it."""
        if position is not None:
            x, y = position
            self._x = 0
            self._y = y + height
            self._band_h = 0
        else:
            if self._x + width > _COLUMNS:
                self._advance_band()
            x, y = self._x, self._y
            self._x += width
            self._band_h = max(self._band_h, height)
        return {"h": height, "w": width, "x": x, "y": y}

    def section(self, *, gap_before: int = 0) -> None:
        """Start a new section: close the current band, apply the historical
        gap, and leave the cursor at the next free row."""
        if self._x or self._band_h:
            self._advance_band()
        self._y += gap_before

    def _advance_band(self) -> None:
        self._y += self._band_h
        self._x = 0
        self._band_h = 0


def _ordered(specs: list[MetricSpec]) -> list[MetricSpec]:
    """Render rank within a section: explicit ``order`` first (ascending),
    then the rest in registration order (a stable sort)."""
    return sorted(specs, key=lambda spec: (spec.order is None, spec.order or 0))


def _size(spec: MetricSpec) -> tuple[int, int]:
    default_width, default_height = _DEFAULT_SIZE.get(spec.panel, (12, 7))
    return spec.width or default_width, spec.height or default_height


def _row(section_title: str, row_id: int, grid: dict[str, int]) -> dict[str, Any]:
    return {
        "collapsed": False,
        "gridPos": grid,
        "id": row_id,
        "panels": [],
        "title": section_title,
        "type": "row",
    }


def render_dashboard(
    core_specs: Iterable[MetricSpec], plugin_specs: Iterable[MetricSpec]
) -> dict[str, Any]:
    """Render the complete dashboard JSON from the two metric registries.

    Only specs with ``grafana`` in ``output`` render. Core panels must carry
    ``section`` (a registered section title) and ``panel_id``; plugin panels
    get their ids allocated per plugin block.
    """
    core = [spec for spec in core_specs if "grafana" in spec.output]
    plugins = [spec for spec in plugin_specs if "grafana" in spec.output]

    core_by_section: dict[str, list[MetricSpec]] = {}
    for spec in core:
        if spec.section is None:
            raise DashboardRenderError(
                f"core metric {spec.name!r} has no section — fill its placement pins"
            )
        if spec.section not in _CORE_SECTION_TITLES:
            raise DashboardRenderError(
                f"core metric {spec.name!r} names unknown section {spec.section!r}"
            )
        if spec.panel_id is None:
            raise DashboardRenderError(
                f"core metric {spec.name!r} has no panel_id — fill its placement pins"
            )
        core_by_section.setdefault(spec.section, []).append(spec)

    plugin_groups: dict[str, list[MetricSpec]] = {}
    for spec in plugins:
        plugin_groups.setdefault(spec.plugin, []).append(spec)

    panels: list[dict[str, Any]] = []
    layout = _Layout()

    def render_core_section(section: _Section) -> None:
        specs = _ordered(core_by_section.get(section.title, []))
        if not specs:
            return
        layout.section(gap_before=section.gap_before)
        panels.append(_row(section.title, section.row_id, layout.place(_COLUMNS, 1)))
        for spec in specs:
            panel_id = spec.panel_id
            if panel_id is None:  # unreachable — the validation above covers it
                raise DashboardRenderError(f"core metric {spec.name!r} has no panel_id")
            width, height = _size(spec)
            panels.append(
                _panel(spec, panel_id, layout.place(width, height, position=spec.position))
            )

    for section in _CORE_SECTIONS_PREFIX:
        render_core_section(section)

    next_id = _PLUGIN_ID_BASE
    for plugin in sorted(plugin_groups):
        specs = _ordered(plugin_groups[plugin])
        layout.section()
        panels.append(_row(plugin, next_id, layout.place(_COLUMNS, 1)))
        next_id += 1
        for spec in specs:
            width, height = _size(spec)
            panels.append(
                _panel(spec, next_id, layout.place(width, height, position=spec.position))
            )
            next_id += 1

    for section in _CORE_SECTIONS_SUFFIX:
        render_core_section(section)

    return {
        "annotations": {"list": []},
        "editable": True,
        "graphTooltip": 1,
        "links": [],
        "panels": panels,
        "preload": True,
        "refresh": "10m",
        "schemaVersion": 39,
        "tags": ["ava", "ops", "grafana-embed"],
        "templating": {"list": []},
        "time": dict(_DEFAULT_RANGE),
        "timepicker": {},
        "timezone": "Asia/Shanghai",
        "title": "Ava Ops",
        "uid": _DASHBOARD_UID,
        "version": 6,
    }


def render_to_json(dashboard: dict[str, Any]) -> str:
    """Deterministic serialization of a rendered dashboard (sorted keys, the
    provisioning file's one-space indent, trailing newline) — byte-identical
    for identical input, so the write path can hash-compare to skip churn."""
    return json.dumps(dashboard, indent=1, sort_keys=True) + "\n"
