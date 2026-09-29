"""SQL whitelist and FROM-clause validation for plugin metrics."""

from __future__ import annotations

import re


class PluginMetricError(Exception):
    """Root of plugin-metric register / validate failures."""


class InvalidMetricQuery(PluginMetricError):  # noqa: N818
    """The query template failed SQL safety validation, or the metric's
    output surfaces contradict its query ({{agent_id}} + grafana)."""


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
