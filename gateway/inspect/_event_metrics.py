"""Inspector event metrics over the event record — the LogQL templates, evaluated on Postgres.

Metric specs write one LogQL template for the Grafana panel and the inspector panel alike. The
inspector no longer asks Loki (whose copy keeps 84 hours): this module reads the same templates
and answers them from `telemetry_events` and `audit_events`, over the inspector's fixed window in
hourly steps. Only the vocabulary the registry's builders write is understood:

    [N *] TERM [/ (TERM | N)]
    TERM := sum(count_over_time({service_name=..., event_name=...} | json | FILTER | ... [W]))
          | sum(sum_over_time({...} | json | FILTER | ... | unwrap attributes_KEY [W]))
    FILTER := FIELD (= | != | =~ | !~) "VALUE"       FIELD := category, level, agent_id, machine,
                                                      source, process, event_name, attributes_KEY

Each step `t` evaluates every TERM over `(t - W, t]`, like Loki's instant range vector. A step with
no rows counts as 0; a ratio step whose denominator is 0 is left out. Anything else raises
`UnsupportedQueryError`, which the endpoint reports on that metric alone.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, LiteralString

import psycopg
from psycopg import sql

_TERM = re.compile(
    r"sum\((?P<fn>count_over_time|sum_over_time)\(\{(?P<selector>[^}]*)\}\s*\|\s*json"
    r"(?P<stages>[^\[]*)\[(?P<window>\d+[smhd])\]\)\)"
)
_SKELETON = re.compile(
    r"^(?:(?P<scale>\d+(?:\.\d+)?) \* )?T0(?: / (?P<divisor>T1|\d+(?:\.\d+)?))?$"
)
_MATCHER = re.compile(r'(\w+)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"')
_UNWRAP = re.compile(r"^unwrap (attributes_\w+)$")
_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_COLUMN_SQL: dict[str, LiteralString] = {
    "level": "e.level",
    "agent_id": "COALESCE(e.agent_id::text, '')",
    "machine": "e.machine",
    "source": "e.source",
    "process": "e.process",
    "event_name": "e.event_name",
}
_NUMBER = r"^-?[0-9]+(\.[0-9]+)?([eE][-+]?[0-9]+)?$"


def _stages(text: str) -> list[str]:
    """The `|`-separated pipeline stages, splitting only outside double quotes."""
    stages: list[str] = []
    current: list[str] = []
    quoted = escaped = False
    for char in text:
        if char == "|" and not quoted:
            stages.append("".join(current).strip())
            current = []
            continue
        current.append(char)
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
    stages.append("".join(current).strip())
    return [stage for stage in stages if stage]


class UnsupportedQueryError(ValueError):
    """The LogQL template uses a construct the Postgres evaluator does not implement."""


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def _matcher_clause(field: str, op: str, value: str) -> tuple[LiteralString, list[Any]]:
    """One `field op "value"` filter as a SQL clause on the event table's columns."""
    if field.startswith("attributes_"):
        expr: LiteralString = "COALESCE(e.attributes ->> %s, '')"
        head: list[Any] = [field.removeprefix("attributes_")]
    elif field in _COLUMN_SQL:
        expr = _COLUMN_SQL[field]
        head = []
    else:
        raise UnsupportedQueryError(f"filter on {field!r} is not supported")
    value = _unescape(value)
    match op:
        case "=":
            return f"{expr} = %s", [*head, value]
        case "!=":
            return f"{expr} <> %s", [*head, value]
        case "=~":
            return f"{expr} ~ %s", [*head, f"^(?:{value})$"]
        case _:
            return f"{expr} !~ %s", [*head, f"^(?:{value})$"]


class _Term:
    """One `sum(count_over_time|sum_over_time(...))` term, ready to run."""

    def __init__(self, match: re.Match[str]) -> None:
        self.window = int(match["window"][:-1]) * _SECONDS[match["window"][-1]]
        clauses: list[tuple[LiteralString, list[Any]]] = []
        for field, op, value in _MATCHER.findall(match["selector"]):
            if field == "event_name":
                clauses.append(_matcher_clause(field, op, value))
        self.audit = False
        self.categories: list[str] | None = None
        self.unwrap: str | None = None
        for stage in _stages(match["stages"]):
            if not stage:
                continue
            unwrap = _UNWRAP.match(stage)
            if unwrap:
                self.unwrap = unwrap[1].removeprefix("attributes_")
                continue
            stage_match = _MATCHER.fullmatch(stage)
            if stage_match is None:
                raise UnsupportedQueryError(f"stage {stage!r} is not supported")
            field, op, value = stage_match.groups()
            if field == "category":
                self._category(op, _unescape(value))
                continue
            clauses.append(_matcher_clause(field, op, value))
        if (match["fn"] == "sum_over_time") != (self.unwrap is not None):
            raise UnsupportedQueryError(
                "sum_over_time needs unwrap, and unwrap needs sum_over_time"
            )
        self.clauses = clauses

    def _category(self, op: str, value: str) -> None:
        names = [value] if op == "=" else value.split("|") if op == "=~" else None
        if names is None:
            raise UnsupportedQueryError("category != and !~ are not supported")
        self.audit = names == ["audit"]
        if "audit" in names and not self.audit:
            raise UnsupportedQueryError("a query over audit and other categories is not supported")
        self.categories = None if self.audit else [name for name in names if name != "audit"]

    def table(self) -> LiteralString:
        return "audit_events" if self.audit else "telemetry_events"

    def value(self) -> tuple[LiteralString, list[Any]]:
        if self.unwrap is None:
            return "count(e.event_uid)", []
        return (
            "COALESCE(sum(CASE WHEN e.attributes ->> %s ~ %s "
            "THEN (e.attributes ->> %s)::float8 END), 0)",
            [self.unwrap, _NUMBER, self.unwrap],
        )

    def evaluate(
        self, conn: psycopg.Connection[Any], start: datetime, stop: datetime, step_s: int
    ) -> dict[datetime, float]:
        """The term's value at every step from `start` to `stop`."""
        filters: list[sql.Composable] = []
        params: list[Any] = []
        for text, values in self.clauses:
            filters.append(sql.SQL(text))
            params.extend(values)
        if self.categories:
            filters.append(sql.SQL("e.category = ANY(%s)"))
            params.append(self.categories)
        value_sql, value_params = self.value()
        query = sql.SQL(
            "SELECT t.at, {value} FROM generate_series(%s::timestamptz, %s::timestamptz, %s::interval) "
            "AS t(at) LEFT JOIN {table} e ON e.ts > t.at - %s::interval AND e.ts <= t.at"
            "{filters} GROUP BY t.at ORDER BY t.at"
        ).format(
            value=sql.SQL(value_sql),
            table=sql.SQL(self.table()),
            filters=sql.SQL("").join([sql.SQL(" AND ") + clause for clause in filters]),
        )
        rows = conn.execute(
            query,
            [
                *value_params,
                start,
                stop,
                timedelta(seconds=step_s),
                timedelta(seconds=self.window),
                *params,
            ],
        ).fetchall()
        return {row[0]: float(row[1]) for row in rows}


def points(
    conn: psycopg.Connection[Any], query: str, start: datetime, stop: datetime, step_s: int
) -> list[tuple[datetime, float]]:
    """Evaluate one LogQL template to `(step time, value)` points; raise `UnsupportedQueryError` otherwise."""
    terms: list[_Term] = []

    def replace(match: re.Match[str]) -> str:
        terms.append(_Term(match))
        return f"T{len(terms) - 1}"

    skeleton = _TERM.sub(replace, query).strip()
    shape = _SKELETON.fullmatch(skeleton)
    if shape is None or not terms:
        raise UnsupportedQueryError(f"query shape {skeleton!r} is not supported")
    scale = float(shape["scale"]) if shape["scale"] else 1.0
    divisor = shape["divisor"]
    if divisor == "T1" and len(terms) != 2:
        raise UnsupportedQueryError("a ratio needs two terms")
    numerator = terms[0].evaluate(conn, start, stop, step_s)
    if divisor is None:
        return [(at, scale * value) for at, value in numerator.items()]
    if divisor == "T1":
        denominator = terms[1].evaluate(conn, start, stop, step_s)
        return [
            (at, scale * value / denominator[at])
            for at, value in numerator.items()
            if denominator.get(at)
        ]
    return [(at, scale * value / float(divisor)) for at, value in numerator.items()]
