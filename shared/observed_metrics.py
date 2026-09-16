"""Compact, idempotent measurements from observed telemetry, never billing completeness.

Only numeric facts enter Postgres. The source event identity is shared with JSONL
and Loki, so replay repairs a missing projection without charging a call twice.
Daily sums and facts commit together; historical readers choose a source per day,
never add these sums to an older ledger of the same observations.

The emitter queue may already have lost records. Neither a successful write nor
a completed replay scan establishes complete collection. A missing usage-time
price remains unpriced; replay never consults today's price catalog.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Any, Literal, LiteralString, TypedDict, cast

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from shared.telemetry import Event, event_row


@dataclass(frozen=True)
class MetricObservation:
    """One source event reduced to a typed additive measurement."""

    event_id: int
    agent_id: int
    occurred_at: datetime
    kind: Literal["usage", "turn", "exec", "activity"]
    model: str | None = None
    usage_calls: int = 0
    unpriced_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    tokens_reasoning: int = 0
    cost_usd: Decimal = Decimal(0)
    turn_total: int = 0
    turn_ok: int = 0
    turn_duration_seconds: float | None = None
    exec_ok: int = 0
    exec_failed: int = 0
    active_seconds: float = 0
    exec_seconds: float = 0


def _number(value: Any) -> float:
    result = float(value)
    if isinstance(value, bool) or not math.isfinite(result) or result < 0:
        raise ValueError("observed metric must be a finite nonnegative number")
    return result


def _count(value: Any) -> int:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or result != result.to_integral_value():
        raise ValueError("observed metric count must be a nonnegative integer")
    return int(result)


def _truth(value: Any) -> bool:
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ValueError("observed turn outcome must be true or false")


class _MetricIdentity(TypedDict):
    event_id: int
    agent_id: int
    occurred_at: datetime


def _activity(attributes: Mapping[str, Any]) -> tuple[float, float]:
    entries = attributes.get("nodes", [attributes])
    if not isinstance(entries, list):
        raise TypeError("observed node batch must be a list")
    active = execution = 0.0
    for raw in cast(list[Any], entries):
        if not isinstance(raw, dict):
            raise TypeError("observed node entry must be an object")
        entry = cast(dict[str, Any], raw)
        node = entry.get("node", "")
        if not isinstance(node, str):
            raise TypeError("observed node name must be text")
        duration = _number(entry["duration_seconds"])
        if node != "claim":
            active += duration
        if node == "exec":
            execution += duration
    return active, execution


def _usage(base: _MetricIdentity, attributes: Mapping[str, Any]) -> MetricObservation:
    """Preserve a usage-time price or explicitly record the call as unpriced."""
    cost = attributes.get("cost_usd")
    unpriced = cost is None or _count(attributes.get("unpriced", 0)) != 0
    price = Decimal(0) if unpriced else Decimal(str(cost))
    if not price.is_finite() or price < 0:
        raise ValueError("observed usage cost must be finite and nonnegative")
    model = attributes.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError("observed usage model must be text")
    return MetricObservation(
        **base,
        kind="usage",
        model=model,
        usage_calls=1,
        unpriced_calls=int(unpriced),
        tokens_in=_count(attributes.get("in_total", 0)),
        tokens_out=_count(attributes.get("out_total", 0)),
        tokens_cached=_count(attributes.get("cache_read", 0)),
        tokens_reasoning=_count(attributes.get("reasoning", 0)),
        cost_usd=price,
    )


def _supports(name: str, category: str, agent_id: int | None) -> bool:
    if agent_id is None:
        return False
    if name in {"llm_usage", "turn_end"}:
        return category in {"telemetry", "log"}
    return name in {"node_exit", "exec"} or name.startswith(("exec_", "exec("))


def observe_row(row: Mapping[str, Any]) -> MetricObservation | None:
    """Reduce a normalized JSONL/Loki row; malformed supported events raise.

    Callers isolate malformed rows explicitly. Unknown event families and events
    without an agent carry no Inspector measurement and are ignored.
    """
    name = row["event_name"]
    if not _supports(name, row["category"], row["agent_id"]):
        return None
    raw_attributes = row["attributes"]
    if not isinstance(raw_attributes, dict):
        raise TypeError("observed event attributes must be an object")
    attributes = cast(dict[str, Any], raw_attributes)
    occurred_at = row["ts"]
    if isinstance(occurred_at, str):
        occurred_at = datetime.fromisoformat(occurred_at)
    if not isinstance(occurred_at, datetime) or occurred_at.tzinfo is None:
        raise ValueError("observed event timestamp must be timezone-aware")
    base: _MetricIdentity = {
        "event_id": _count(row["id"]),
        "agent_id": _count(row["agent_id"]),
        "occurred_at": occurred_at.astimezone(UTC),
    }
    if name == "llm_usage":
        return _usage(base, attributes)
    if name == "turn_end":
        duration = attributes.get("duration_seconds")
        return MetricObservation(
            **base,
            kind="turn",
            turn_total=1,
            turn_ok=int(_truth(attributes.get("ok", False))),
            turn_duration_seconds=_number(duration) if duration is not None else None,
        )
    if name == "node_exit":
        active, execution = _activity(attributes)
        return MetricObservation(
            **base, kind="activity", active_seconds=active, exec_seconds=execution
        )
    return MetricObservation(
        **base, kind="exec", exec_ok=int(name == "exec"), exec_failed=int(name != "exec")
    )


def observe_event(event: Event) -> MetricObservation | None:
    """Use exactly the identity serialized to the event's JSONL mirror."""
    # Ordinary logs can hold large bodies. Do not serialize them a second time
    # for a projection that never consumes their contents.
    if not _supports(event.event_name, event.category, event.agent_id):
        return None
    return observe_row(event_row(event))


_COUNTERS = (
    "usage_calls",
    "unpriced_calls",
    "tokens_in",
    "tokens_out",
    "tokens_cached",
    "tokens_reasoning",
    "turn_total",
    "turn_ok",
    "exec_ok",
    "exec_failed",
)
_SUMS = (*_COUNTERS, "cost_usd", "active_seconds", "exec_seconds")
_RECORD_TYPES = {
    "event_id": "numeric",
    "agent_id": "bigint",
    "occurred_at": "timestamptz",
    "kind": "text",
    "model": "text",
    **dict.fromkeys(_COUNTERS, "bigint"),
    "cost_usd": "numeric",
    "turn_duration_seconds": "double precision",
    "active_seconds": "double precision",
    "exec_seconds": "double precision",
}
_FIELDS = ", ".join(_RECORD_TYPES)
_DAY_FIELDS = (
    "agent_id, day, "
    + ", ".join(_SUMS)
    + ", turn_duration_sum, turn_duration_min, turn_duration_max, last_observed_at"
)
# Insert and aggregate in one statement: ON CONFLICT returns only novel facts,
# including when two processes concurrently replay the same source batch.
_WRITE_SQL = f"""
WITH inserted AS (
    INSERT INTO agent_metric_observations ({_FIELDS})
    SELECT {_FIELDS} FROM jsonb_to_recordset(%s) AS facts (
        {", ".join(f"{name} {type_}" for name, type_ in _RECORD_TYPES.items())}
    ) ORDER BY event_id
    ON CONFLICT (event_id) DO NOTHING
    RETURNING *
), rolled_up AS (
    INSERT INTO agent_metric_days ({_DAY_FIELDS})
    SELECT agent_id, (occurred_at AT TIME ZONE 'UTC')::date,
        {", ".join(f"sum({name})" for name in _SUMS)},
        coalesce(sum(turn_duration_seconds), 0),
        min(turn_duration_seconds), max(turn_duration_seconds), max(observed_at)
    FROM inserted GROUP BY agent_id, (occurred_at AT TIME ZONE 'UTC')::date
    ORDER BY agent_id, (occurred_at AT TIME ZONE 'UTC')::date
    ON CONFLICT (agent_id, day) DO UPDATE SET
        {", ".join(f"{name} = agent_metric_days.{name} + EXCLUDED.{name}" for name in _SUMS)},
        turn_duration_sum = agent_metric_days.turn_duration_sum + EXCLUDED.turn_duration_sum,
        turn_duration_min = least(agent_metric_days.turn_duration_min, EXCLUDED.turn_duration_min),
        turn_duration_max = greatest(agent_metric_days.turn_duration_max, EXCLUDED.turn_duration_max),
        last_observed_at = greatest(agent_metric_days.last_observed_at, EXCLUDED.last_observed_at)
    RETURNING agent_id
) SELECT count(*) FROM inserted
"""  # noqa: S608 — identifiers come exclusively from static constants.

_pool: ConnectionPool | None = None
_pool_lock = Lock()


def _projection_pool() -> ConnectionPool:
    """One lazily opened, bounded pool owned by this process's drain thread."""
    global _pool  # noqa: PLW0603
    with _pool_lock:
        if _pool is None:
            from shared.db import pool

            _pool = pool(min_size=0, max_size=1, timeout=0.1)
        return _pool


def close_projection() -> None:
    """Close the sink only after the emitter has drained its final batch."""
    global _pool  # noqa: PLW0603
    with _pool_lock:
        if _pool is not None:
            _pool.close(timeout=0.5)
            _pool = None


def write_observations(
    observations: Sequence[MetricObservation], *, db: psycopg.Connection[Any] | None = None
) -> int:
    """Atomically store novel facts and day sums; return number newly inserted.

    The optional connection belongs to replay/tests; its caller owns commit.
    The live emitter uses a separate pool and a short transaction deadline.
    Exceptions propagate here so replay never records a failed scan as success;
    only the emitter adapter below suppresses them.
    """
    if not observations:
        return 0
    if db is None:
        from shared.db_transaction import write_transaction

        with write_transaction(_projection_pool(), timeout=0.1) as connection:
            connection.execute("SET LOCAL statement_timeout = '500ms'")
            connection.execute("SET LOCAL lock_timeout = '100ms'")
            return write_observations(observations, db=connection)
    rows: list[dict[str, Any]] = []
    for observation in observations:
        row = asdict(observation)
        row["occurred_at"] = observation.occurred_at.isoformat()
        row["cost_usd"] = str(observation.cost_usd)
        rows.append(row)
    result = db.execute(cast(LiteralString, _WRITE_SQL), (Jsonb(rows),)).fetchone()
    if result is None:
        raise RuntimeError("observed metric insert returned no count")
    return int(result[0])


_failures = 0


def project_events(events: Sequence[Event]) -> None:
    """Best-effort emitter sink; diagnostics must never reenter the emitter."""
    global _failures  # noqa: PLW0603
    from shared.telemetry import _report_no_pipeline

    observations: list[MetricObservation] = []
    rejected = 0
    for event in events:
        try:
            observation = observe_event(event)
            if observation is not None:
                observations.append(observation)
        except Exception:
            rejected += 1
    if rejected:
        _report_no_pipeline(
            "[observed-metrics] rejected {n} malformed measurement(s); collection is partial",
            n=rejected,
        )
    try:
        write_observations(observations)
    except Exception as exc:
        _failures += 1
        if _failures == 1 or _failures % 50 == 0:
            _report_no_pipeline(
                "[observed-metrics] projection failed ({n} consecutive): {err}; "
                "JSONL replay may repair observed rows, collection remains partial",
                n=_failures,
                err=repr(exc),
            )
    else:
        _failures = 0
