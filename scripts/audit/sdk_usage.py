"""Rank real SDK calls from JSONL event mirrors and suggest a stable expansion list.

Read-only: this script never changes cluster configuration. Copy the desired
event mirrors from each runner, then pass an explicit timezone-aware window.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field


class CallAttributes(BaseModel):
    fn: str = Field(pattern=r"^(ava\.)?[A-Za-z_]\w*(\.\w+)*$")
    sample_rate: int = Field(default=1, strict=True, ge=1)


class CallEvent(BaseModel):
    id: str | int
    ts: AwareDatetime
    agent_id: int | None
    attributes: CallAttributes


def timestamp(value: str) -> datetime:
    """Parse an ISO timestamp without accepting an implicit local timezone."""
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamps must include a timezone offset")
    return result


def read_calls(paths: list[Path]) -> list[CallEvent]:
    """Read recorded call events, deduplicating copied mirrors by event identity."""
    events: dict[str, CallEvent] = {}
    for path in dict.fromkeys(paths):
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                    if raw["event_name"] != "sdk_call":
                        continue
                    event = CallEvent.model_validate(raw)
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError(f"{path}:{line_number}: {exc}") from exc
                key = str(event.id)
                if key in events and events[key] != event:
                    raise ValueError(f"{path}:{line_number}: conflicting SDK event identity {key}")
                events[key] = event
    return list(events.values())


def _rank_modules(
    counts: Counter[str], agents: dict[str, set[int]], coverage: float
) -> tuple[list[str], int, list[dict[str, Any]]]:
    """Group methods at the module boundary and choose cumulative coverage."""
    modules: Counter[str] = Counter()
    module_agents: dict[str, set[int]] = defaultdict(set)
    for fn, count in counts.items():
        if "." in fn:
            module = fn.split(".", 1)[0]
            modules[module] += count
            module_agents[module].update(agents[fn])
    eligible = sum(n for m, n in modules.items() if m not in {"skills", "mcps"})
    cumulative = 0
    selected: list[str] = []
    ranking: list[dict[str, Any]] = []
    for module, count in sorted(modules.items(), key=lambda item: (-item[1], item[0])):
        expandable = module not in {"skills", "mcps"}
        if expandable:
            if cumulative < eligible * coverage / 100:
                selected.append(module)
            cumulative += count
        ranking.append(
            {
                "module": module,
                "weighted_calls": count,
                "observed_agents": len(module_agents[module]),
                "expandable": expandable,
                "cumulative_eligible_pct": 100 * cumulative / eligible if eligible else None,
            }
        )
    return selected, eligible, ranking


def report(
    events: list[CallEvent],
    start: datetime,
    end: datetime,
    coverage: float = 70,
    agent_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Select whole modules until cumulative eligible call volume reaches coverage.

    Sampling weights estimate event volume; distinct-agent counts remain observed
    lower bounds. Skills/MCPs are capability catalogs, and top-level functions are
    already described by the overview, so none earns a full SDK expansion here.
    """
    if start.tzinfo is None or end.tzinfo is None or start > end:
        raise ValueError("window must be timezone-aware and start <= end")
    if not 0 < coverage <= 100:
        raise ValueError("coverage must be greater than 0 and at most 100")
    counts: Counter[str] = Counter()
    agents: dict[str, set[int]] = defaultdict(set)
    observed = sampled = 0
    for event in events:
        if not start <= event.ts <= end:
            continue
        if agent_ids is not None and event.agent_id not in agent_ids:
            continue
        observed += 1
        sampled += event.attributes.sample_rate > 1
        fn = event.attributes.fn.removeprefix("ava.")
        counts[fn] += event.attributes.sample_rate
        if event.agent_id is not None:
            agents[fn].add(event.agent_id)
    selected, eligible, ranking = _rank_modules(counts, agents, coverage)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "observed_events": observed,
        "sampled_events": sampled,
        "weighted_calls": sum(counts.values()),
        "eligible_module_calls": eligible,
        "coverage_target_pct": coverage,
        "selected_sdk_modules": selected,
        "suggested_ava_sdk_expand": ",".join(selected) if selected else None,
        "modules": ranking,
        "methods": [
            {"method": fn, "weighted_calls": n}
            for fn, n in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "limitations": "Recorded events only; sampling and missing mirrors limit coverage. "
        "Inspect observed-agent reach and workload mix before adopting a default. "
        "Plugin-declared SDK expansions still apply.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", nargs="+", type=Path, help="JSONL event mirror files")
    parser.add_argument("--start", required=True, type=timestamp)
    parser.add_argument("--end", required=True, type=timestamp)
    parser.add_argument("--coverage", type=float, default=70, help="cumulative call percentage")
    parser.add_argument("--agent-id", type=int, action="append", help="repeat to narrow callers")
    args = parser.parse_args(argv)
    try:
        data = report(
            read_calls(args.events),
            args.start,
            args.end,
            args.coverage,
            set(args.agent_id) if args.agent_id is not None else None,
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
