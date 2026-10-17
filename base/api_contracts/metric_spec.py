"""The plugin metric spec: one declared metric over the event stream or a permitted SQL source.

A plugin declares its metrics as `MetricSpec` values (its `metrics.py` exports `contribute()`
returning a `PluginContributions` with `metrics`). The spec is plain data: fields, their
patterns and the cross-field checks that need no query dialect. This module imports pydantic and
nothing first-party, so every layer that only names the type (the plugin contribution value, the
SDK installation) can import it without loading the telemetry stack. Query safety (SQL, LogQL,
PromQL), rendering and registration live in `base.telemetry.metrics.plugin_metrics`, which
re-exports `MetricSpec` and `ThresholdStep` as the plugin-author entry point.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "TIME_BASIS_LABEL",
    "Category",
    "MetricSpec",
    "OutputSurface",
    "PanelType",
    "ThresholdStep",
    "TimeBasis",
    "bucket_minutes",
]

Category = Literal["audit", "telemetry", "log"]
PanelType = Literal["timeseries", "stat", "barchart", "table", "logs"]
OutputSurface = Literal["grafana", "inspector"]
TimeBasis = Literal["per_minute", "window"]

TIME_BASIS_LABEL: dict[TimeBasis, str] = {"per_minute": "per minute", "window": "window"}
_BUCKET_WINDOW = re.compile(r"\[(\d+)m\]")
_TRAILING_DIVISOR = re.compile(r"/\s*\d+\s*$")


class ThresholdStep(BaseModel):
    """One Grafana absolute-threshold step (``fieldConfig.defaults.thresholds``).

    ``value: None`` is the base step (covers everything below the next step);
    a non-None value flips the color at that point. Mirrors the steps shape in
    ``deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json``.
    """

    model_config = ConfigDict(frozen=True)

    color: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9-]*$")
    value: float | None = None


class MetricSpec(BaseModel):
    """One plugin metric over the live event stream or a permitted SQL source.

    Fields:
        name: globally unique id (``^[a-z][a-z0-9_]*$``). Convention:
            ``<plugin>_<what>`` (e.g. ``meiva_syntax_fix_rate``).
        title: Grafana panel title.
        description: what the metric measures / its event provenance.
        event_name: the event name the query filters on — lowercase letters,
            digits, ``_`` and ``-`` (hyphens appear in the live vocabulary,
            e.g. ``recall-filter``).
        category: the event category (audit | telemetry | log).
        unit: Grafana unit id (``short``, ``percent``, ``ops``, ``s``, ...).
        panel: Grafana panel type — ``timeseries`` / ``stat`` / ``barchart`` / ``table`` / ``logs``.
            A ``logs`` panel is a raw stream view whose LogQL skips the
            ``{event_name}``/``{category}`` placeholder rule (the ``raw_view``
            waiver; only the stream-selector and ``| json`` checks apply).
        query: Grafana query template. LogQL templates select the live event
            stream and use ``{event_name}`` / ``{category}`` placeholders;
            ``{{agent_id}}`` is inspector-only and rendered as a label filter.
        targets: extra query templates rendered as refId B/C/... targets on
            the same panel (multi-series panels — e.g. the core TPS panels'
            max/min-agent series). Validated like ``query``.
        options / custom / field_defaults: optional panel-look overrides
            merged into the generated panel's ``options`` /
            ``fieldConfig.defaults.custom`` / ``fieldConfig.defaults`` (the
            generator's defaults win for keys not present here). An empty
            ``thresholds`` list (``[]``) suppresses the default green-base
            step entirely (panels without any thresholds).
        thresholds: optional absolute-threshold steps (green base + red at the
            given value by default when a bare number list would suffice).
        panel_id / section / order / position: dashboard placement pins
            (2026-09-17, task #3697) — the as-is panel id, the section row it
            belongs to, the panel's render rank within that section, and an
            explicit grid position for the rare panel whose placement
            deviates from the flow layout. Core panels carry the as-is
            values; plugin panels leave them None (the renderer allocates
            ids per plugin block and uses the plugin name as the section).
        transformations: optional Grafana transformations copied verbatim
            into the panel (e.g. the PR-flow day tables' joinByField +
            organize), for the panels whose rendered shape needs them.
        time_basis: how the number relates to time, rendered by the registry
            so the title and the query cannot drift (panel titles state their
            time basis — user ruling 2026-09-14). ``per_minute``: every query
            part is one ``[Nm]`` bucket aggregate written without a divisor;
            rendering appends `` / N`` and the title gains ``(per minute)``.
            ``window``: every part aggregates over ``$__range`` and the title
            gains ``(window)``. None when the unit already says it (``/
            minute``, TPS) or the basis is something else (a trailing gauge).
        output: which surfaces consume this metric — ``grafana`` (dashboard
            JSON, this wave), ``inspector`` (per-agent panels, reserved).
            A query carrying ``{{agent_id}}`` must NOT include ``grafana``.
        plugin: filled by the data registry from the declaring plugin's entry — never set
            it yourself.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=120)
    description: str = ""
    event_name: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    category: Category
    unit: str = "short"
    panel: PanelType = "timeseries"
    query: str = Field(min_length=1)
    # Query dialect: "sql" (postgres datasource over live state tables such
    # as `agents_meta`), "logql" (Loki event stream, #1280), or static "promql"
    # (Prometheus gauges/counters; Grafana-only).
    query_type: Literal["sql", "logql", "promql"] = "sql"
    # Additional query targets (refIds B/C/...) for multi-series panels —
    # each target is validated and rendered exactly like `query`.
    targets: list[str] | None = None
    # Legend names for the rendered targets (refIds A, B, ...), one per
    # series. LogQL/PromQL aggregates carry no labels, so Grafana targets
    # render these as legendFormat values; SQL targets are named by their
    # column aliases and must not combine with target_names.
    target_names: list[str] | None = None
    # Optional panel look overrides, merged into the generated panel JSON:
    # `options` merges into panel "options" (legend/colorMode/noValue/...),
    # `custom` merges into fieldConfig.defaults.custom (stacking/axisLabel/
    # fillOpacity/...). Core panels use these to keep their exact rendered
    # look after migrating from hand-written JSON (2026-08-06, Task #882).
    options: dict[str, Any] | None = None
    custom: dict[str, Any] | None = None
    # Overrides merged into fieldConfig.defaults itself (e.g. the stat
    # color mode) — keys not present here keep the generator defaults.
    field_defaults: dict[str, Any] | None = None
    # Explicit grid size (override the 6x4 stat / 12x7 chart default).
    width: int | None = Field(default=None, ge=1, le=24)
    height: int | None = Field(default=None, ge=1, le=40)
    # Dashboard placement pins (task #3697): the as-is panel id, its section
    # row, the render rank within that section, and an explicit grid position
    # for the rare panel whose placement deviates from the flow layout. Core
    # panels carry the as-is values; plugin panels leave them None (the
    # renderer allocates ids per plugin block and uses the plugin name as the
    # section).
    panel_id: int | None = Field(default=None, ge=1)
    section: str | None = None
    order: int | None = Field(default=None, ge=0)
    position: tuple[int, int] | None = None
    # Grafana transformations copied verbatim into the rendered panel (the
    # PR-flow day tables' joinByField + organize).
    transformations: list[dict[str, Any]] | None = None
    thresholds: list[ThresholdStep] | None = None
    time_basis: TimeBasis | None = None
    output: list[OutputSurface] = ["grafana"]
    plugin: str = ""  # filled when the data registry admits the declaration

    @field_validator("output")
    @classmethod
    def _output_valid(cls, v: list[OutputSurface]) -> list[OutputSurface]:
        if not v:
            raise ValueError("output must list at least one surface")
        seen: set[str] = set()
        for surface in v:
            if surface in seen:
                raise ValueError(f"output lists {surface!r} twice")
            seen.add(surface)
        return v

    @model_validator(mode="after")
    def _target_names_consistent(self) -> MetricSpec:
        if self.target_names is None:
            return self
        if self.query_type not in ("logql", "promql"):
            raise ValueError(
                "target_names requires query_type='logql' or 'promql' (SQL targets are "
                "named by their column aliases)"
            )
        expected = 1 + len(self.targets or [])
        if len(self.target_names) != expected:
            raise ValueError(
                f"target_names must name every rendered target ({expected} names, "
                f"got {len(self.target_names)})"
            )
        return self

    @model_validator(mode="after")
    def _time_basis_consistent(self) -> MetricSpec:
        if self.time_basis is None:
            return self
        label = TIME_BASIS_LABEL[self.time_basis]
        if re.search(rf"\([^)]*\b{label}\b[^)]*\)", self.title):
            raise ValueError(
                f"title spells the time basis {label!r} by hand; time_basis renders it"
            )
        parts = [self.query, *(self.targets or [])]
        if self.time_basis == "window":
            if not all("$__range" in part for part in parts):
                raise ValueError(
                    "time_basis='window' needs every query part to aggregate over $__range"
                )
            return self
        if self.query_type == "sql":
            raise ValueError("time_basis='per_minute' needs a logql or promql query")
        for part in parts:
            bucket_minutes(part)
        return self

    @model_validator(mode="after")
    def _promql_is_grafana_only(self) -> MetricSpec:
        """PromQL core tiles have no gateway inspector execution path yet."""

        if self.query_type == "promql" and self.output != ["grafana"]:
            raise ValueError("query_type='promql' is currently supported only for grafana output")
        return self


def bucket_minutes(template: str) -> int:
    """The bucket width of a per-minute query part: its one ``[Nm]`` window.

    Exactly one window keeps the part a single aggregation, so the appended
    `` / N`` divides all of it; a part that already divides is refused so the
    divisor has one owner.
    """
    windows = _BUCKET_WINDOW.findall(template)
    if len(windows) != 1:
        raise ValueError(
            "time_basis='per_minute' needs exactly one [Nm] bucket window per query "
            f"part, found {len(windows)}: {template!r}"
        )
    if _TRAILING_DIVISOR.search(template):
        raise ValueError(
            f"query part already divides by its bucket; time_basis renders the divisor: {template!r}"
        )
    return int(windows[0])
