"""Plugin metric registration — plugins declare metrics over the event stream.

Two output surfaces (user-approved design, 2026-08-04, event-system W13):

- ``grafana``: the ops dashboards
  (``deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-*.json``) are
  hand-maintained since the generator did not survive the archive->public
  port — a MetricSpec change must be mirrored in the JSONs by hand
  (``tests/plugins/test_plugin_metrics_logql.py`` locks JSON against the
  registered specs).
- ``inspector`` (W13b): the gateway builds the registry in process (imports
  every plugin's ``metrics.py`` under its PluginContext + the core definition
  modules — task #180 PR D) and serves per-agent panels under
  ``/api/agents/{id}/inspect/metrics``. Query templates may carry the
  ``{{agent_id}}`` placeholder, which the gateway renders per dialect
  (``agent_id="<n>"`` for LogQL, ``agent_id = <n>`` for SQL).

Registration mirrors the plugin state/config pattern: the plugin calls
``register_metric(MetricSpec(...))`` at import time inside ``PluginContext``
(the framework ``_load_extensions`` wrap) and the plugin name is auto-filled.
The registry is process-local.

SQL safety (enforced at register time, task #180 PR C): a metric query must
be a static single SELECT over ``events`` (the frozen archive) or
``agents_meta`` (live), built from a whitelist of keywords, aggregate
functions, operators and literals — no DML/DDL, no information/``pg_*``
functions, no comments, no multi-statement, no Grafana macros, no
``{event_name}`` / ``{category}`` / ``{{agent_id}}`` placeholders (the live
event stream is read through LogQL, task #1280). LogQL templates
(``query_type="logql"``) follow the lighter contract in
``shared/metrics_logql.py``. PromQL templates (``query_type="promql"``)
are static Prometheus expressions.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.plugin_context import current_plugin_name

Category = Literal["audit", "telemetry", "log"]
PanelType = Literal["timeseries", "stat", "barchart", "table"]
OutputSurface = Literal["grafana", "inspector"]

# ── errors ────────────────────────────────────────────────────────────────────


class PluginMetricError(Exception):
    """Root of plugin-metric register / validate failures."""


class NoPluginContext(PluginMetricError):  # noqa: N818 — parallel to plugin_config_registry's NoPluginContext
    """``register_metric`` called outside PluginContext — the framework wraps
    plugin imports, so this is a plugin authoring bug (or a test calling
    register directly)."""


class DuplicateMetric(PluginMetricError):  # noqa: N818
    """Two metrics registered under the same ``name`` — names are global
    (across every plugin), so the author should prefix with the plugin."""


class InvalidMetricQuery(PluginMetricError):  # noqa: N818
    """The query template failed SQL safety validation, or the metric's
    output surfaces contradict its query ({{agent_id}} + grafana)."""


# ── spec ──────────────────────────────────────────────────────────────────────


class ThresholdStep(BaseModel):
    """One Grafana absolute-threshold step (``fieldConfig.defaults.thresholds``).

    ``value: None`` is the base step (covers everything below the next step);
    a non-None value flips the color at that point. Mirrors the steps shape in
    ``deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json``.
    """

    model_config = ConfigDict(frozen=True)

    color: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9-]*$")
    value: float | None = None


class MetricSpec(BaseModel):
    """One plugin metric over the live event stream or a permitted SQL source.

    Fields:
        name: globally unique id (``^[a-z][a-z0-9_]*$``). Convention:
            ``<plugin>_<what>`` (e.g. ``ava_code_syntax_fix_rate``).
        title: Grafana panel title.
        description: what the metric measures / its event provenance.
        event_name: the event name the query filters on — lowercase letters,
            digits, ``_`` and ``-`` (hyphens appear in the live vocabulary,
            e.g. ``recall-filter``).
        category: the event category (audit | telemetry | log).
        unit: Grafana unit id (``short``, ``percent``, ``ops``, ``s``, ...).
        panel: Grafana panel type — ``timeseries`` / ``stat`` / ``barchart`` / ``table``.
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
        output: which surfaces consume this metric — ``grafana`` (dashboard
            JSON, this wave), ``inspector`` (per-agent panels, reserved).
            A query carrying ``{{agent_id}}`` must NOT include ``grafana``.
        plugin: filled by ``register_metric`` from PluginContext — never set
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
    # Query dialect: "sql" (postgres datasource over the legacy `events`
    # table), "logql" (loki event stream), or "promql" (Prometheus).
    query_type: Literal["sql", "logql", "promql"] = "sql"
    # Additional query targets (refIds B/C/...) for multi-series panels —
    # each target is validated and rendered exactly like `query`.
    targets: list[str] | None = None
    # Legend names for the rendered targets (refIds A, B, ...), one per
    # series. LogQL and PromQL aggregates carry no labels, so Grafana targets
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
    thresholds: list[ThresholdStep] | None = None
    output: list[OutputSurface] = ["grafana"]
    plugin: str = ""  # auto-filled at register time

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
        if self.query_type == "sql":
            raise ValueError(
                "target_names requires query_type='logql' or 'promql' (SQL "
                "targets are named by their column aliases)"
            )
        expected = 1 + len(self.targets or [])
        if len(self.target_names) != expected:
            raise ValueError(
                f"target_names must name every rendered target ({expected} names, "
                f"got {len(self.target_names)})"
            )
        return self


# ── SQL safety validation ─────────────────────────────────────────────────────

# Keywords allowed anywhere in the template (upper-compared). Anything else
# that is not a whitelisted function / macro / column / operator / literal is
# rejected. Subqueries are allowed (FROM (SELECT ...) FROM events ...) — every
# FROM clause is still required to reference only `events`.
_SQL_KEYWORDS = frozenset(
    {
        "SELECT",
        "FROM",
        "WHERE",
        "GROUP",
        "BY",
        "ORDER",
        "LIMIT",
        "OFFSET",
        "AS",
        "AND",
        "OR",
        "NOT",
        "IN",
        "LIKE",
        "IS",
        "NULL",
        "TRUE",
        "FALSE",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "FILTER",
        "WITHIN",
        "LEFT",
        "JOIN",
        "ON",
    }
)

# Explicitly denied words: information keywords and set/compound-statement
# operators. They are denied even though they would be harmless against the
# locked `events` table — the template contract is a single plain SELECT, and
# keeping the grammar small makes the whitelist auditable.
_SQL_DENIED_KEYWORDS = frozenset(
    {
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "WITH",
        "HAVING",
        "TABLESAMPLE",
        "CURRENT_USER",
        "CURRENT_ROLE",
        "CURRENT_SCHEMA",
        "CURRENT_DATE",
        "CURRENT_TIME",
        "CURRENT_TIMESTAMP",
        "SESSION_USER",
        "SYSTEM_USER",
        "USER",
        "CURRENT_CATALOG",
    }
)

# Aggregate / scalar functions that are read-only and safe on the events table.
# percentile_cont (with WITHIN GROUP) is the ordered-set aggregate used for
# per-bucket duration percentiles (W18 ava_observability pack).
_SQL_FUNCTIONS = frozenset(
    {
        "COUNT",
        "SUM",
        "AVG",
        "MIN",
        "MAX",
        "NULLIF",
        "COALESCE",
        "ROUND",
        "PERCENTILE_CONT",
        # Core-dashboard set/date helpers (Task #882 migration; all read-only):
        "TO_TIMESTAMP",
        "FLOOR",
        "EXTRACT",
        "MAKE_INTERVAL",
        # Set-returning helper allowed in FROM (the FROM checker restricts
        # where it may appear; the call itself is read-only).
        "GENERATE_SERIES",
    }
)

# Grafana Postgres macros. `$__interval_ms` is used by the ops dashboard for
# tokens/s; `$__timeGroup` / `$__timeFilter` are the standard window macros.
# events-table columns (plus `time`, the conventional $__timeGroup alias).
_SQL_COLUMNS = frozenset(
    {
        "ts",
        "trace_id",
        "span_id",
        "agent_id",
        "machine",
        "process",
        "category",
        "event_name",
        "level",
        "source",
        "target_agent_id",
        "attributes",
        "id",
        "time",
        "events",
    }
)

# Operators / punctuation. `->`, `->>`, `?`, `@>`, `<@`, `#>`, `#>>` are the
# JSONB access/containment operators used by the existing dashboards
# (the `latency_ms` / `status` payload keys).
_SQL_OPS = frozenset(
    {
        "(",
        ")",
        ",",
        ".",
        "+",
        "-",
        "*",
        "/",
        "%",
        "=",
        "<>",
        "!=",
        "<",
        "<=",
        ">",
        ">=",
        "::",
        "->",
        "->>",
        "?",
        "@>",
        "<@",
        "#>",
        "#>>",
        "=>",
    }
)

# A quoted identifier ("...") — the alias escape hatch Grafana SQL uses for
# column names with spaces (e.g. "max agent"). Content restricted to printable
# ASCII to keep the rendered JSON predictable; `<`, `>`, `/` are allowed so
# display labels like "stalled <60s" / "tokens/s" stay verbatim (2026-08-06,
# Task #882 core-panel migration).
_DOUBLE_QUOTED_OK = re.compile(r"^[A-Za-z0-9 _%+\-.:<>/]{1,64}$")

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<num>\d+(?:\.\d+)?)
    | (?P<str>'(?:[^']|'')*')
    | (?P<ident>"(?:[^"]*)")
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<op>::|->>|->|[#]>|[#]>>|@>|<@|\?|<=|>=|<>|!=|[()+,.*%/:=<>-])
    """,
    re.VERBOSE,
)
_QUOTED_RE = re.compile(r"\'(?:[^\']|\'\')*\'|\"(?:[^\"])*\"")


_FROM_FOLLOW = frozenset({"WHERE", "GROUP", "ORDER", "LIMIT", "OFFSET"})
# Words that may legally follow a table reference without being its alias:
# WHERE/GROUP/... (clause keywords), LEFT (join), ON (join condition), or a
# comma (next table in a comma list). Anything else after `events` /
# `agents_meta` is a rejected table alias.
_TABLE_END = _FROM_FOLLOW | {"LEFT", "ON"}
_ALLOWED_FROM_TABLES = frozenset({"EVENTS", "AGENTS_META"})
# Set-returning functions allowed in FROM (checked call-by-call, args still
# pass through the normal token whitelist).
_FROM_FUNCTIONS = frozenset({"GENERATE_SERIES"})


def _skip_paren(tokens: list[tuple[str, str]], j: int, sql: str) -> int:
    """tokens[j] must be ``(``; return the index just past the matching ``)``."""
    depth = 0
    while j < len(tokens):
        if tokens[j] == ("op", "("):
            depth += 1
        elif tokens[j] == ("op", ")"):
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise InvalidMetricQuery(f"unbalanced parentheses in FROM clause: {sql!r}")


def _consume_alias_and_cols(
    tokens: list[tuple[str, str]], j: int, sql: str, offset: int, alias_positions: set[int]
) -> int:
    """Consume an optional table alias (``AS g`` or bare ``g``) plus an
    optional column list ``(time)`` — record the alias position, recurse."""
    if j < len(tokens) and tokens[j][0] == "word" and tokens[j][1].upper() == "AS":
        j += 1
    if j < len(tokens) and tokens[j][0] == "word" and tokens[j][1].upper() not in _TABLE_END:
        alias_positions.add(offset + j)
        j += 1
        if j < len(tokens) and tokens[j] == ("op", "("):
            cols_start = j
            j = _skip_paren(tokens, j, sql)
            alias_positions |= _check_from_clauses(
                tokens[cols_start + 1 : j - 1],
                sql,
                offset=offset + cols_start + 1,
            )
    return j


def _check_table_ref(
    tokens: list[tuple[str, str]], j: int, sql: str, offset: int
) -> tuple[int, set[int]]:
    """Validate one table reference (after FROM or a comma) and return
    (index just past it, global positions of aliases consumed). Aliases
    followed by a column list ``AS g(time)`` must be exempted from the
    main walk's function-call check — the alias position is recorded."""
    alias_positions: set[int] = set()
    if j >= len(tokens):
        raise InvalidMetricQuery(f"FROM without a table name: {sql!r}")
    if tokens[j] == ("op", "("):
        inner_start = j
        j = _skip_paren(tokens, j, sql)
        alias_positions |= _check_from_clauses(
            tokens[inner_start + 1 : j - 1], sql, offset=offset + inner_start + 1
        )
        # optional subquery alias (bare word or AS word) + optional column list
        j = _consume_alias_and_cols(tokens, j, sql, offset, alias_positions)
    elif tokens[j][0] == "word" and tokens[j][1].upper() in _ALLOWED_FROM_TABLES:
        j += 1
        if j < len(tokens) and tokens[j][0] == "word" and tokens[j][1].upper() not in _TABLE_END:
            raise InvalidMetricQuery(
                f"table alias after `{tokens[j - 1][1].lower()}` is not allowed "
                f"(use a column alias instead): {sql!r}"
            )
    elif tokens[j][0] == "word" and tokens[j][1].upper() in _FROM_FUNCTIONS:
        if j + 1 >= len(tokens) or tokens[j + 1] != ("op", "("):
            raise InvalidMetricQuery(
                f"FROM function {tokens[j][1].upper()!r} must be called with arguments: {sql!r}"
            )
        # Recurse into the argument list: a subquery smuggled into the args
        # (generate_series((SELECT 1 FROM pg_class))) must not escape FROM
        # validation; function-call args inside (extract(epoch FROM ...)) are
        # skipped by the recursive walk's function-call rule.
        args_start = j + 1
        j = _skip_paren(tokens, args_start, sql)
        alias_positions |= _check_from_clauses(
            tokens[args_start + 1 : j - 1], sql, offset=offset + args_start + 1
        )
        # Optional alias: `AS g` or bare `g`, with an optional column list
        # `(time)`.
        j = _consume_alias_and_cols(tokens, j, sql, offset, alias_positions)
    else:
        raise InvalidMetricQuery(
            f"FROM must reference only the `events` table, got {tokens[j][1]!r}: {sql!r}"
        )
    return j, alias_positions


def _check_from_item(
    tokens: list[tuple[str, str]], j: int, sql: str, offset: int
) -> tuple[int, set[int]]:
    """Validate one FROM item (table + comma list + LEFT JOIN chain) and
    return (index just past it, alias positions consumed)."""
    j, alias_positions = _check_table_ref(tokens, j, sql, offset)
    while j < len(tokens):
        if tokens[j] == ("op", ","):
            j, more = _check_table_ref(tokens, j + 1, sql, offset)
            alias_positions |= more
            continue
        if tokens[j][0] == "word" and tokens[j][1].upper() == "LEFT":
            if (
                j + 1 >= len(tokens)
                or tokens[j + 1][0] != "word"
                or tokens[j + 1][1].upper() != "JOIN"
            ):
                raise InvalidMetricQuery(f"LEFT must be followed by JOIN: {sql!r}")
            j += 2
            if j >= len(tokens) or tokens[j] != ("op", "("):
                raise InvalidMetricQuery(
                    f"LEFT JOIN must reference a subquery (bare-table "
                    f"joins are not allowed): {sql!r}"
                )
            inner_start = j
            j = _skip_paren(tokens, j, sql)
            alias_positions |= _check_from_clauses(
                tokens[inner_start + 1 : j - 1],
                sql,
                offset=offset + inner_start + 1,
            )
            # optional alias (bare word, not a clause keyword / ON)
            if (
                j < len(tokens)
                and tokens[j][0] == "word"
                and tokens[j][1].upper() not in (_FROM_FOLLOW | {"ON"})
            ):
                alias_positions.add(offset + j)
                j += 1
            # optional ON condition — consume until a depth-0 clause keyword
            # (WHERE/GROUP/ORDER/LIMIT/OFFSET) or end; parens recurse.
            if j < len(tokens) and tokens[j][0] == "word" and tokens[j][1].upper() == "ON":
                j += 1
                while j < len(tokens):
                    if tokens[j] == ("op", "("):
                        inner_start = j
                        j = _skip_paren(tokens, j, sql)
                        alias_positions |= _check_from_clauses(
                            tokens[inner_start + 1 : j - 1],
                            sql,
                            offset=offset + inner_start + 1,
                        )
                    elif tokens[j][0] == "word" and tokens[j][1].upper() in _FROM_FOLLOW:
                        break
                    else:
                        j += 1
            continue
        break
    return j, alias_positions


def _check_from_clauses(
    tokens: list[tuple[str, str]],
    sql: str,
    offset: int = 0,
) -> set[int]:
    """Walk every FROM clause; each table reference must be the ``events``
    table, ``agents_meta``, a parenthesized subquery (checked recursively),
    or a whitelisted set-returning function call (``generate_series``).
    ``LEFT JOIN`` is allowed only onto a subquery (never a bare table), with
    an optional alias + ON condition whose tokens pass the normal whitelist.

    The walk skips function-call argument lists (``extract(epoch FROM ...)``
    — a FROM word inside a call is not a clause) and recurses into every
    other parenthesis (subqueries anywhere, e.g. a stray
    ``JOIN (SELECT 1 FROM x)``).

    Returns the global positions of FROM aliases — the caller exempts them
    from the function-call check in the main token classification (an alias
    column list ``AS g(time)`` must not read as a call ``g(...)``).
    """
    alias_positions: set[int] = set()
    i = 0
    while i < len(tokens):
        kind, value = tokens[i]
        if kind == "word" and value.upper() == "FROM":
            j, more = _check_from_item(tokens, i + 1, sql, offset)
            alias_positions |= more
            i = j
            continue
        if (
            kind == "word"
            and i + 1 < len(tokens)
            and tokens[i + 1] == ("op", "(")
            and value.upper() in _SQL_FUNCTIONS
        ):
            # function call — the argument list itself is not a FROM clause
            # (extract(epoch FROM ts) is legal), but a parenthesized
            # subquery inside the args IS legal SQL (scalar subquery,
            # coalesce((SELECT ...), 0)) and its FROM must be validated —
            # otherwise `pg_class` smuggles past the events-only gate.
            # Scan the args and recurse into every nested paren group.
            args_end = _skip_paren(tokens, i + 1, sql)
            k = i + 2
            while k < args_end - 1:
                if tokens[k] == ("op", "("):
                    inner_end = _skip_paren(tokens, k, sql)
                    alias_positions |= _check_from_clauses(
                        tokens[k + 1 : inner_end - 1],
                        sql,
                        offset=offset + k + 1,
                    )
                    k = inner_end
                else:
                    k += 1
            i = args_end
            continue
        if kind == "op" and value == "(":
            # non-function paren (subquery / stray paren) — recurse
            j = _skip_paren(tokens, i, sql)
            alias_positions |= _check_from_clauses(
                tokens[i + 1 : j - 1], sql, offset=offset + i + 1
            )
            i = j
            continue
        i += 1
    return alias_positions


def validate_metric_sql(sql: str) -> None:
    """Validate one static metric query against the read-only whitelist.

    Task #180 (PR C): the SQL template era is over — the live event stream is
    read through LogQL (task #1280), so Grafana time macros and the
    ``{event_name}`` / ``{category}`` / ``{{{{agent_id}}}}`` placeholders are
    rejected outright. What remains is the static-read guardrail: a single
    SELECT over ``events`` (the frozen archive — deliberate archive reads
    only) or ``agents_meta`` (live), whitelisted functions / columns /
    operators, no comments / multi-statement / DML / information functions.

    Rejects: multi-statement input (``;`` outside the trailing position),
    comments (``--`` / ``/* */``), any FROM target other than ``events`` /
    ``agents_meta``, Grafana macros, template placeholders, unknown
    identifiers / functions / operators, and any character the tokenizer
    does not recognize (e.g. dollar-quoted strings).

    Raises:
        InvalidMetricQuery: with a concrete reason for plugin authors.
    """
    # 1. Single-statement / comment / no-template checks on a string with
    #    quoted literals blanked out (so 'a;b' or "--" inside a string does
    #    not trip them).
    body = sql.rstrip()
    if body.endswith(";"):
        body = body[:-1]
    blanked = _QUOTED_RE.sub("''", body)
    if "--" in blanked or "/*" in blanked:
        raise InvalidMetricQuery(
            f"SQL comments are not allowed in metric queries (found '--' or '/* */'): {sql!r}"
        )
    if ";" in blanked:
        raise InvalidMetricQuery(
            f"multi-statement SQL is not allowed — exactly one SELECT, no ';' separators: {sql!r}"
        )
    if "$__" in body:
        raise InvalidMetricQuery(
            "Grafana time macros are not allowed in metric queries — the live "
            f"event stream is read through LogQL (task #1280/#180): {sql!r}"
        )
    if "{" in body or "}" in body:
        raise InvalidMetricQuery(
            "template placeholders are not allowed in metric queries — "
            "{event_name}/{category}/{{agent_id}} were retired with the "
            f"events-table cutover (task #180): {sql!r}"
        )

    # 2. Tokenize; every character must belong to a recognized token.
    tokens: list[tuple[str, str]] = []  # (kind, value)
    pos = 0
    for m in _TOKEN_RE.finditer(body):
        if m.start() != pos:
            bad = body[pos : m.start()]
            raise InvalidMetricQuery(
                f"unrecognized character(s) {bad!r} in metric query template — "
                f"only whitelisted SQL constructs are allowed: {sql!r}"
            )
        kind = m.lastgroup
        assert kind is not None  # noqa: S101 — regex always names a group
        if kind != "ws":
            tokens.append((kind, m.group(kind)))
        pos = m.end()
    if pos != len(body):
        raise InvalidMetricQuery(
            f"unrecognized trailing character(s) {body[pos:]!r} in metric query template: {sql!r}"
        )

    # 3. Must start with SELECT — case-insensitive, like every other keyword
    #    check in this module (the whitelists all upper-compare), so a plugin
    #    author writing `select ...` or `Select ...` is not rejected.
    if not tokens or tokens[0][0] != "word" or tokens[0][1].upper() != "SELECT":
        raise InvalidMetricQuery(
            "metric query template must be a single SELECT statement, got "
            f"{tokens[0] if tokens else '(empty)'}: {sql!r}"
        )

    # 4. Every FROM clause must reference only the `events` table (or a
    #    subquery, whose inner FROM clauses are checked recursively). Comma-
    #    separated table lists are walked item by item; a bare word right
    #    after a table/subquery is treated as its alias (harmless — the
    #    identifier whitelist below still applies to everything else).
    #    FROM aliases are exempted from the function-call check below (an
    #    alias column list `AS g(time)` must not read as a call `g(...)`).
    from_alias_positions = _check_from_clauses(tokens, sql)

    # 5. Classify every remaining token against the whitelists.
    for i, (kind, value) in enumerate(tokens):
        if kind in ("num", "str"):
            continue
        if kind == "ident":
            inner = value[1:-1]
            if not _DOUBLE_QUOTED_OK.match(inner):
                raise InvalidMetricQuery(
                    f"quoted identifier {value!r} contains characters outside "
                    f"the allowed set: {sql!r}"
                )
            continue
        if kind == "op":
            if value not in _SQL_OPS:
                raise InvalidMetricQuery(f"operator {value!r} is not on the whitelist: {sql!r}")
            continue
        assert kind == "word"  # noqa: S101
        upper = value.upper()
        if upper in _SQL_DENIED_KEYWORDS:
            raise InvalidMetricQuery(
                f"keyword {value!r} is not allowed in metric query templates "
                f"(denied set: {sorted(_SQL_DENIED_KEYWORDS)}): {sql!r}"
            )
        if upper in _SQL_KEYWORDS or upper in _SQL_FUNCTIONS or value in _SQL_COLUMNS:
            continue
        # A bare word that is not whitelisted is only allowed as a column
        # reference / alias WITHOUT a call — a following `(` means a function
        # invocation, and only the whitelisted functions may be called
        # (version(), pg_sleep(), now(), ... are rejected here). Column/alias
        # names resolve against the locked `events` table at query time, so a
        # misspelled one fails the query itself, never executes anything.
        if i + 1 < len(tokens) and tokens[i + 1] == ("op", "(") and i not in from_alias_positions:
            raise InvalidMetricQuery(
                f"function call {value!r}(...) is not on the whitelist — only "
                f"{sorted(_SQL_FUNCTIONS)} may be called: {sql!r}"
            )
        continue


# ── registry ──────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, MetricSpec] = {}


def register_metric(spec: MetricSpec) -> MetricSpec:
    """Register one metric — must run inside PluginContext (the framework
    wraps plugin imports).

    Validation at register time: name uniqueness across all plugins and
    query safety (``validate_metric_sql`` / ``validate_spec_logql``, by
    dialect). The ``plugin`` field is auto-filled from the context,
    overriding whatever the author passed.

    Raises:
        NoPluginContext: called outside ``with PluginContext(...)``.
        DuplicateMetric: ``spec.name`` already registered.
        InvalidMetricQuery: a query template failed validation.
    """
    plugin = current_plugin_name()
    if plugin is None:
        raise NoPluginContext(
            "register_metric() must be called inside PluginContext — the "
            "framework `_load_extensions` already wraps plugin imports; the "
            "generator wraps metrics-module imports with the plugin name."
        )
    if spec.name in _REGISTRY:
        raise DuplicateMetric(
            f"metric {spec.name!r} already registered (by plugin "
            f"{_REGISTRY[spec.name].plugin!r}) — names are global, prefix with "
            f"the plugin name."
        )
    validate_spec_sql(spec)
    filled = spec.model_copy(update={"plugin": plugin})
    _REGISTRY[spec.name] = filled
    return filled


def validate_spec_sql(spec: MetricSpec) -> None:
    """Validate every query template on a spec (``query`` + ``targets``) —
    the static-SQL whitelist, LogQL contract, or PromQL sanity check, by dialect. Shared by
    ``register_metric`` and ``register_core_metric`` (Task #882) — core
    metrics go through the same safety checks as plugin metrics. SQL
    templates carry no placeholders anymore (task #180 PR C), so the old
    ``{{agent_id}}`` ↔ grafana rule is subsumed by the placeholder
    rejection; the LogQL dialect keeps its ``{{agent_id}}`` inspector idiom
    (render-only, per-agent). PromQL has neither event-stream requirements
    nor template substitutions."""
    if spec.query_type == "logql":
        # Lazy: metrics_logql imports this module (the exception class), so a
        # module-level from-import here would cycle.
        from shared.metrics_logql import validate_spec_logql

        validate_spec_logql(spec)
        return
    if spec.query_type == "promql":
        for template in [spec.query, *(spec.targets or [])]:
            if any(
                placeholder in template
                for placeholder in ("{event_name}", "{category}", "{{agent_id}}")
            ):
                raise InvalidMetricQuery(
                    "PromQL metric queries cannot use event-stream template placeholders: "
                    f"{template!r}"
                )
        return
    for template in [spec.query, *(spec.targets or [])]:
        validate_metric_sql(template)


def registered_metrics() -> list[MetricSpec]:
    """All registered metrics, in registration order (grouped by plugin for
    the generator's row layout)."""
    return list(_REGISTRY.values())


def clear_registry() -> None:
    """Drop every registration — test fixtures (parallel to
    ``agent.state.clear_plugin_registrations``)."""
    _REGISTRY.clear()


# ── rendering + export ────────────────────────────────────────────────────────


def _sql_literal(value: str) -> str:
    """Render a validated identifier/enum as a single-quoted SQL literal.
    Kind/category are validated to ``^[a-z][a-z0-9_]*$`` / a Literal at spec
    construction, so the escaping is defense in depth."""
    return "'" + value.replace("'", "''") + "'"


def _render_template(template: str, spec: MetricSpec, agent_id: int | None) -> str:
    """Substitute ``{event_name}`` / ``{category}`` and, when ``agent_id``
    is given, ``{{agent_id}}``. The literal quoting follows the dialect:
    SQL wants single quotes, LogQL wants double quotes; PromQL is static."""
    if spec.query_type == "promql":
        return template
    literal = _sql_literal if spec.query_type == "sql" else _logql_literal
    rendered = template.replace("{event_name}", literal(spec.event_name)).replace(
        "{category}", literal(spec.category)
    )
    # {category_re} renders the category UNQUOTED — for embedding inside an
    # already-quoted LogQL regex (e.g. category=~"{category_re}|log" ->
    # category=~"telemetry|log"). SQL templates never use it (no regex
    # literals); the sql validator rejects the bare value either way.
    if spec.query_type == "logql":
        rendered = rendered.replace("{category_re}", spec.category)
    if agent_id is not None:
        if spec.query_type == "logql":
            rendered = rendered.replace("{{agent_id}}", f'agent_id="{int(agent_id)}"')
        else:
            rendered = rendered.replace("{{agent_id}}", f"agent_id = {int(agent_id)}")
    return rendered


def _logql_literal(value: str) -> str:
    """Double-quoted LogQL string literal (stream/line-filter values)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_query(spec: MetricSpec, agent_id: int | None = None) -> str:
    """Render the primary template (``query``) for one surface.

    ``{event_name}`` / ``{category}`` are substituted with single-quoted literals
    (the generator writes static JSON — no parameter binding available).
    ``{{agent_id}}`` stays verbatim unless ``agent_id`` is passed (the
    inspector surface renders it to ``agent_id = <n>``).
    """
    return _render_template(spec.query, spec, agent_id)


def render_targets(spec: MetricSpec, agent_id: int | None = None) -> list[str]:
    """Render every SQL template on the spec (``query`` first, then each
    ``targets`` entry) — one Grafana target per series group."""
    return [render_query(spec, agent_id)] + [
        _render_template(t, spec, agent_id) for t in (spec.targets or [])
    ]
