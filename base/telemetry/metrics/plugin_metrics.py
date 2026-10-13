"""Plugin metric registration — plugins declare metrics over the event stream.

Two output surfaces (user-approved design, 2026-08-04, event-system W13):

- ``grafana``: the ops dashboard
  (``deploy/lgtm/config/grafana/provisioning/dashboards/ava-ops-main.json``)
  is becoming a render of the registry — ``base.telemetry.metrics.grafana_dashboard`` turns
  the specs into the dashboard JSON and ``ava lgtm render`` previews / writes
  it (task #3697; slice S3 flips converge onto the render). Until then the
  JSON stays the deployment source, and
  ``tests/components/plugins/test_grafana_dashboard_render.py`` locks the render against
  it.
- ``inspector`` (W13b): the gateway builds the registry in process (loads
  every plugin's ``metrics.py`` declaration + the core definition
  modules — task #180 PR D) and serves per-agent panels under
  ``/api/agents/{id}/inspect/metrics``. Query templates may carry the
  ``{{agent_id}}`` placeholder, which the gateway renders per dialect
  (``agent_id="<n>"`` for LogQL, ``agent_id = <n>`` for SQL).

A plugin declares its metrics as data: its ``metrics.py`` exports ``contribute()``, a pure
function returning a ``PluginContributions`` with ``metrics``. The data registry
(``base/packages/plugins/data_registry.py``) validates each spec (query safety below, name
uniqueness across plugins) and fills ``plugin`` from the registry entry; nothing is registered
process-wide.

SQL safety (enforced when the data registry admits a plugin, task #180 PR C): a metric query must
be a static single SELECT over ``agents_meta`` (live), built from a whitelist of keywords, aggregate
functions, operators and literals — no DML/DDL, no information/``pg_*``
functions, no comments, no multi-statement, no Grafana macros, no
``{event_name}`` / ``{category}`` / ``{{agent_id}}`` placeholders. The event
stream is not in Postgres: SQL templates read live state tables only, and event
metrics use ``query_type="logql"`` / ``"promql"``. LogQL templates
(``query_type="logql"``) follow the lighter contract in
``base/telemetry/metrics/logql.py``. PromQL templates (``query_type="promql"``)
are static Prometheus expressions.
"""

from __future__ import annotations

from base.api_contracts.metric_spec import (
    TIME_BASIS_LABEL,
    bucket_minutes,
)
from base.api_contracts.metric_spec import (
    MetricSpec as MetricSpec,
)
from base.api_contracts.metric_spec import (
    ThresholdStep as ThresholdStep,
)
from base.telemetry.metrics.logql import validate_logql_template
from base.telemetry.metrics.plugin_metrics_sql import (
    DOUBLE_QUOTED_OK,
    QUOTED_RE,
    SQL_DENIED_KEYWORDS,
    SQL_FUNCTIONS,
    SQL_KEYWORDS,
    SQL_OPS,
    TOKEN_RE,
    InvalidMetricQuery,
    PluginMetricError,
    check_from_clauses,
)

# ── errors ────────────────────────────────────────────────────────────────────


class DuplicateMetric(PluginMetricError):  # noqa: N818
    """Two metrics declared under the same ``name`` — names are global
    (across every plugin), so the author should prefix with the plugin."""


# ── SQL safety validation ─────────────────────────────────────────────────────


def _single_statement_body(sql: str) -> str:
    """`sql` minus its trailing `;`, after the single-statement / comment / no-template checks."""
    # 1. Single-statement / comment / no-template checks on a string with
    #    quoted literals blanked out (so 'a;b' or "--" inside a string does
    #    not trip them).
    body = sql.rstrip()
    if body.endswith(";"):
        body = body[:-1]
    blanked = QUOTED_RE.sub("''", body)
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
            "{event_name}/{category}/{{agent_id}} are not supported in SQL "
            f"templates: {sql!r}"
        )
    return body


def _tokenize_metric_sql(sql: str, body: str) -> list[tuple[str, str]]:
    """Tokenize; every character must belong to a recognized token."""
    tokens: list[tuple[str, str]] = []  # (kind, value)
    pos = 0
    for m in TOKEN_RE.finditer(body):
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
    return tokens


def _require_select(tokens: list[tuple[str, str]], sql: str) -> None:
    # 3. Must start with SELECT — case-insensitive, like every other keyword
    #    check in this module (the whitelists all upper-compare), so a plugin
    #    author writing `select ...` or `Select ...` is not rejected.
    if not tokens or tokens[0][0] != "word" or tokens[0][1].upper() != "SELECT":
        raise InvalidMetricQuery(
            "metric query template must be a single SELECT statement, got "
            f"{tokens[0] if tokens else '(empty)'}: {sql!r}"
        )


def _classify_tokens(
    tokens: list[tuple[str, str]], sql: str, from_alias_positions: set[int]
) -> None:
    """Classify every remaining token against the whitelists."""
    for i, (kind, value) in enumerate(tokens):
        if kind in ("num", "str"):
            continue
        if kind == "ident":
            inner = value[1:-1]
            if not DOUBLE_QUOTED_OK.match(inner):
                raise InvalidMetricQuery(
                    f"quoted identifier {value!r} contains characters outside "
                    f"the allowed set: {sql!r}"
                )
            continue
        if kind == "op":
            if value not in SQL_OPS:
                raise InvalidMetricQuery(f"operator {value!r} is not on the whitelist: {sql!r}")
            continue
        assert kind == "word"  # noqa: S101
        _check_word(tokens, i, sql, from_alias_positions)


def _check_word(
    tokens: list[tuple[str, str]], i: int, sql: str, from_alias_positions: set[int]
) -> None:
    """One bare word: a denied keyword raises; an unlisted word may only be a column / alias."""
    value = tokens[i][1]
    upper = value.upper()
    if upper in SQL_DENIED_KEYWORDS:
        raise InvalidMetricQuery(
            f"keyword {value!r} is not allowed in metric query templates "
            f"(denied set: {sorted(SQL_DENIED_KEYWORDS)}): {sql!r}"
        )
    if upper in SQL_KEYWORDS or upper in SQL_FUNCTIONS:
        return
    # A bare word that is not whitelisted is only allowed as a column
    # reference / alias WITHOUT a call — a following `(` means a function
    # invocation, and only the whitelisted functions may be called
    # (version(), pg_sleep(), now(), ... are rejected here). Column/alias
    # names resolve against the `agents_meta` table at query time, so a
    # misspelled one fails the query itself, never executes anything.
    if i + 1 < len(tokens) and tokens[i + 1] == ("op", "(") and i not in from_alias_positions:
        raise InvalidMetricQuery(
            f"function call {value!r}(...) is not on the whitelist — only "
            f"{sorted(SQL_FUNCTIONS)} may be called: {sql!r}"
        )


def validate_metric_sql(sql: str) -> None:
    """Validate one static metric query against the read-only whitelist.

    Task #180 (PR C): the SQL template era is over — the live event stream is
    read through LogQL (task #1280), so Grafana time macros and the
    ``{event_name}`` / ``{category}`` / ``{{{{agent_id}}}}`` placeholders are
    rejected outright. What remains is the static-read guardrail: a single
    SELECT over ``agents_meta`` (live), whitelisted functions /
    operators, no comments / multi-statement / DML / information functions.

    Rejects: multi-statement input (``;`` outside the trailing position),
    comments (``--`` / ``/* */``), any FROM target other than
    ``agents_meta``, Grafana macros, template placeholders, unknown
    identifiers / functions / operators, and any character the tokenizer
    does not recognize (e.g. dollar-quoted strings).

    Raises:
        InvalidMetricQuery: with a concrete reason for plugin authors.
    """
    body = _single_statement_body(sql)
    tokens = _tokenize_metric_sql(sql, body)
    _require_select(tokens, sql)

    # 4. Every FROM clause must reference only the `agents_meta` table (or a
    #    subquery, whose inner FROM clauses are checked recursively). Comma-
    #    separated table lists are walked item by item; a bare word right
    #    after a table/subquery is treated as its alias (harmless — the
    #    identifier whitelist below still applies to everything else).
    #    FROM aliases are exempted from the function-call check below (an
    #    alias column list `AS g(time)` must not read as a call `g(...)`).
    from_alias_positions = check_from_clauses(tokens, sql)

    _classify_tokens(tokens, sql, from_alias_positions)


def validate_spec_sql(spec: MetricSpec) -> None:
    """Validate every query template on a spec (``query`` + ``targets``) —
    the static-SQL whitelist, LogQL contract, or PromQL sanity check, by dialect. Shared by
    ``build_data_registry`` and ``validate_core_metric`` (Task #882) — core
    metrics go through the same safety checks as plugin metrics. SQL
    templates carry no placeholders anymore (task #180 PR C), so the old
    ``{{agent_id}}`` ↔ grafana rule is subsumed by the placeholder
    rejection; the LogQL dialect keeps its ``{{agent_id}}`` inspector idiom
    (render-only, per-agent). PromQL has neither event-stream requirements
    nor template substitutions."""
    if spec.query_type == "logql":
        for template in [spec.query, *(spec.targets or [])]:
            validate_logql_template(template, spec.name, raw_view=spec.panel == "logs")
        return
    if spec.query_type == "promql":
        for template in [spec.query, *(spec.targets or [])]:
            _validate_promql(template)
        return
    for template in [spec.query, *(spec.targets or [])]:
        validate_metric_sql(template)


def _validate_promql(template: str) -> None:
    """Keep Grafana-only PromQL static and single-expression.

    Prometheus is the evaluator; metric registrations only need to prevent a
    dashboard template from accidentally inheriting SQL/LogQL placeholders or
    carrying a multi-statement string. The current use is absolute OTLP gauges.
    """

    if ";" in template or "\n" in template:
        raise InvalidMetricQuery("PromQL metric must be one expression without semicolons/newlines")
    if any(
        token in template
        for token in ("{event_name}", "{category}", "{category_re}", "{{agent_id}}")
    ):
        raise InvalidMetricQuery("PromQL metric must not contain metric-template placeholders")


# ── rendering + export ────────────────────────────────────────────────────────


def sql_literal(value: str) -> str:
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
    literal = sql_literal if spec.query_type == "sql" else _logql_literal
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


def _render_part(template: str, spec: MetricSpec, agent_id: int | None) -> str:
    rendered = _render_template(template, spec, agent_id)
    if spec.time_basis == "per_minute":
        rendered += f" / {bucket_minutes(template)}"
    return rendered


def render_query(spec: MetricSpec, agent_id: int | None = None) -> str:
    """Render the primary template (``query``) for one surface.

    ``{event_name}`` / ``{category}`` are substituted with single-quoted literals
    (the generator writes static JSON — no parameter binding available).
    ``{{agent_id}}`` stays verbatim unless ``agent_id`` is passed (the
    inspector surface renders it to ``agent_id = <n>``). A ``per_minute``
    spec's bucket divisor is appended here.
    """
    return _render_part(spec.query, spec, agent_id)


def render_targets(spec: MetricSpec, agent_id: int | None = None) -> list[str]:
    """Render every template on the spec (``query`` first, then each
    ``targets`` entry) — one Grafana target per series group."""
    return [render_query(spec, agent_id)] + [
        _render_part(t, spec, agent_id) for t in (spec.targets or [])
    ]


def render_title(spec: MetricSpec) -> str:
    """The panel title every surface shows: ``title`` plus the time basis."""
    if spec.time_basis is None:
        return spec.title
    return f"{spec.title} ({TIME_BASIS_LABEL[spec.time_basis]})"
