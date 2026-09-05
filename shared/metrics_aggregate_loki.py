"""Injected Loki query layer for metrics aggregation."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Protocol

from shared.sdk_telemetry import SDK_CALL_SAMPLE_EVERY

_MAX_WORKERS = 4


class LokiBackend(Protocol):
    """The slice of `gateway.loki_events` `fetch_aggregate` needs — injected
    (shared code must not import gateway; tests pass a fake). Signatures
    mirror `gateway.loki_events` 1:1 so the module satisfies the protocol
    structurally (pyright checks call compatibility both ways)."""

    def count_events(
        self,
        *,
        agent_id: int | None = None,
        exclude_agent_ids: list[int] | None = None,
        service_only: bool = False,
        event_names: list[str] | None = None,
        level_min: str | None = None,
        level: str | None = None,
        grep: str | None = None,
        categories: list[str] | None = None,
        machine: str | None = None,
        trace_id: str | None = None,
        attribute_filters: dict[str, str] | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> int: ...

    def count_grouped(
        self,
        *,
        group_by: str,
        from_attributes: bool = False,
        exclude_empty: bool = False,
        agent_id: int | None = None,
        exclude_agent_ids: list[int] | None = None,
        service_only: bool = False,
        event_names: list[str] | None = None,
        level_min: str | None = None,
        level: str | None = None,
        grep: str | None = None,
        categories: list[str] | None = None,
        machine: str | None = None,
        trace_id: str | None = None,
        attribute_filters: dict[str, str] | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> dict[str, int]: ...

    def query_events(
        self,
        *,
        agent_id: int | None = None,
        exclude_agent_ids: list[int] | None = None,
        service_only: bool = False,
        event_names: list[str] | None = None,
        level_min: str | None = None,
        level: str | None = None,
        grep: str | None = None,
        categories: list[str] | None = None,
        machine: str | None = None,
        trace_id: str | None = None,
        attribute_filters: dict[str, str] | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
        direction: str = "backward",
    ) -> tuple[list[dict[str, Any]], bool]: ...

    def query_projected_lines(
        self,
        *,
        fields: list[str],
        template: str,
        agent_id: int | None = None,
        exclude_agent_ids: list[int] | None = None,
        service_only: bool = False,
        event_names: list[str] | None = None,
        level_min: str | None = None,
        level: str | None = None,
        grep: str | None = None,
        categories: list[str] | None = None,
        machine: str | None = None,
        trace_id: str | None = None,
        attribute_filters: dict[str, str] | None = None,
        from_: datetime | None = None,
        to: datetime | None = None,
        limit_per_slice: int = 5000,
    ) -> list[tuple[int, int | None, str]]: ...


# projected-line templates (\x1f separator: payload values never contain it)
_SEP = "\x1f"
_T_BODY_LEN = "{{ len .body }}"
_T_TURN_DUR = "{{.duration_seconds}}"
_T_LLM = _SEP.join(
    f"{{{{.{f}}}}}" for f in ("model", "in_total", "out_total", "cache_read", "reasoning")
)
_T_EVENT_EXC = "{{.event_name}}" + _SEP + "{{.exc_type}}"
_T_EVENT_FIXES = "{{.event_name}}" + _SEP + "{{.fixes}}"
_T_SPAWNER = "{{.spawner}}"
_T_BODY = "{{.body}}"
_T_PLUGIN_ACT = _SEP.join(f"{{{{.{f}}}}}" for f in ("plugin", "surface", "identifier", "model"))

_EXEC_FAIL_NAMES = ["^exec_", "^exec\\("]  # mirrors _AGG_EXEC_FAIL: exec_* or exec(..., excl 'exec'
_LIFECYCLE_NAMES = ["agent_spawned", "agent_terminated", "agent_restarted", "agent_resurrected"]


def _aggregate_tasks(loki: LokiBackend) -> dict[str, Callable[[dict[str, Any]], Any]]:
    """One query task per aggregate — each takes one partition's filter kwargs."""
    return {
        "total": lambda p: loki.count_events(**p),
        "code_blocks": lambda p: loki.count_events(event_names=["code"], **p),
        "exec_ok": lambda p: loki.count_events(event_names=["exec"], **p),
        "turn_total": lambda p: loki.count_events(event_names=["turn_end"], **p),
        "turn_ok": lambda p: loki.count_events(
            event_names=["turn_end"], attribute_filters={"ok": "true"}, **p
        ),
        "idle_halts": lambda p: loki.count_events(
            event_names=["halt"], attribute_filters={"body": "no tool_call (idle)"}, **p
        ),
        "lifecycle": lambda p: loki.count_grouped(
            group_by="event_name", event_names=_LIFECYCLE_NAMES, **p
        ),
        "fail_rows": lambda p: loki.query_projected_lines(
            fields=["exc_type"], template=_T_EVENT_EXC, event_names=_EXEC_FAIL_NAMES, **p
        ),
        "code_len": lambda p: loki.query_projected_lines(
            fields=["body"], template=_T_BODY_LEN, event_names=["code"], **p
        ),
        "output_len": lambda p: loki.query_projected_lines(
            fields=["body"], template=_T_BODY_LEN, event_names=["exec", *_EXEC_FAIL_NAMES], **p
        ),
        "turn_dur": lambda p: loki.query_projected_lines(
            fields=["duration_seconds"], template=_T_TURN_DUR, event_names=["turn_end"], **p
        ),
        "llm": lambda p: loki.query_projected_lines(
            fields=["model", "in_total", "out_total", "cache_read", "reasoning"],
            template=_T_LLM,
            event_names=["llm_usage"],
            **p,
        ),
        "by_agent_all": lambda p: loki.count_grouped(group_by="agent_id", **p),
        "by_agent_code": lambda p: loki.count_grouped(
            group_by="agent_id", event_names=["code"], **p
        ),
        "by_agent_turn": lambda p: loki.count_grouped(
            group_by="agent_id", event_names=["turn_end"], **p
        ),
        "by_agent_turn_ok": lambda p: loki.count_grouped(
            group_by="agent_id", event_names=["turn_end"], attribute_filters={"ok": "true"}, **p
        ),
        "by_agent_exec": lambda p: loki.count_grouped(
            group_by="agent_id", event_names=["exec"], **p
        ),
        "by_agent_exec_fail": lambda p: loki.count_grouped(
            group_by="agent_id", event_names=_EXEC_FAIL_NAMES, **p
        ),
        "spawners": lambda p: loki.query_projected_lines(
            fields=["spawner"], template=_T_SPAWNER, event_names=["agent_spawned"], **p
        ),
        "sdk_fns": lambda p: {
            key: count * SDK_CALL_SAMPLE_EVERY
            for key, count in loki.count_grouped(
                group_by="fn",
                from_attributes=True,
                exclude_empty=True,
                event_names=["sdk_call"],
                **p,
            ).items()
        },
        "fix": lambda p: loki.query_projected_lines(
            fields=["fixes"],
            template=_T_EVENT_FIXES,
            event_names=["code", "syntax_fix"],
            **p,
        ),
        "plugin_act": lambda p: loki.query_projected_lines(
            fields=["plugin", "surface", "identifier", "model"],
            template=_T_PLUGIN_ACT,
            event_names=["plugin_activation"],
            **p,
        ),
    }


def _run_partitioned(
    parts: list[dict[str, Any]],
    tasks: dict[str, Callable[[dict[str, Any]], Any]],
) -> dict[str, list[Any]]:
    """Run every task over every partition in a bounded thread pool; Loki
    errors propagate (fail fast). Result lists are ordered by partition."""
    results: dict[str, list[Any]] = {t: [] for t in tasks}
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
        futs = {
            (_pi, tname): ex.submit(fn, part)
            for _pi, part in enumerate(parts)
            for tname, fn in tasks.items()
        }
        for (_pi, tname), fut in futs.items():
            results[tname].append(fut.result())
    return results


def _merge_counts(vals: list[int]) -> int:
    return sum(vals)


def _merge_groups(vals: list[dict[str, int]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in vals:  # partitions are disjoint by construction
        out.update(v)
    return out


def _merge_rows(vals: list[list[tuple[int, int | None, str]]]) -> list[tuple[int, int | None, str]]:
    return [r for v in vals for r in v]
