"""Report recorded LLM usage for chosen agents, a time window, and birth lineage.

Run on Ava's Python with cluster database access. Optional polling sends a
one-shot budget reminder to explicitly selected peers; it never terminates them.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from base.db import Database
from base.telemetry.metrics.usage import usage_report


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("window timestamps must include a timezone")
    return result.astimezone(UTC)


def positive_id(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError("agent IDs must be positive integers")
    return result


def budget_message(report: dict[str, Any], *, usd_limit: float | None) -> str | None:
    """A reminder leaves the receiving agent in charge of graceful disposition."""
    if usd_limit is not None and (
        isinstance(usd_limit, bool) or not math.isfinite(usd_limit) or usd_limit <= 0
    ):
        raise ValueError("USD limit must be positive and finite")
    totals = report["totals"]
    if usd_limit is None or totals["recorded_cost_usd"] < usd_limit:
        return None
    ids = [agent["agent_id"] for agent in report["agents"]]
    return (
        f"Usage budget reminder: recorded USD {totals['recorded_cost_usd']:.6f} "
        f">= {usd_limit}. Agents: {ids}; "
        f"lineage: {report['lineage']}; window: [{report['start']}, {report['end']}). "
        f"Unpriced calls: {totals['unpriced_calls']}. Reassess before adding work. "
        "Preserve useful results and recovery notes; finish a safe in-flight unit, "
        "prepare a handoff, narrow remaining work, or request a revised budget as appropriate. "
        "This reminder has not terminated any agent or granted additional spending."
    )


def notify_agents(recipients: Sequence[int], message: str) -> None:
    """Notify only named peers, through the existing core communication API."""
    import ava.agents

    for agent_id in sorted(set(recipients)):
        ava.agents.send_message(agent_id, message)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", type=positive_id, action="append", required=True)
    parser.add_argument("--lineage", choices=("self", "spawn", "fork", "all"), default="self")
    window = parser.add_mutually_exclusive_group(required=True)
    window.add_argument("--start", type=parse_time)
    window.add_argument(
        "--lifetime", action="store_true", help="Folded lifetime ledger plus current event tail"
    )
    parser.add_argument("--end", type=parse_time, help="Default: current time on each poll")
    parser.add_argument("--usd-limit", type=float)
    parser.add_argument("--notify-agent", type=positive_id, action="append", default=[])
    parser.add_argument(
        "--poll-seconds",
        type=float,
        help="Poll until breach; bound lifetime with a watcher timeout",
    )
    args = parser.parse_args(argv)
    validate_args(args, parser)
    return args


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.poll_seconds is not None:
        if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0:
            parser.error("--poll-seconds must be positive and finite")
        if args.end or not args.notify_agent or args.usd_limit is None:
            parser.error("polling requires a moving end, a limit, and notification recipients")
    if args.notify_agent and args.usd_limit is None:
        parser.error("notification requires --usd-limit")
    if args.lifetime and args.end:
        parser.error("--lifetime cannot end at a historical timestamp")
    # Validate thresholds before database access, even on an empty report.
    budget_message(
        {"totals": {"total_tokens": 0, "recorded_cost_usd": 0}},
        usd_limit=args.usd_limit,
    )


def main() -> None:
    args = parse_args()
    database = Database.from_settings()
    while True:
        with database.connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            report = usage_report(
                conn,
                roots=args.agent_id,
                lineage=args.lineage,
                start=args.start,
                end=args.end or datetime.now(UTC),
            )
        message = budget_message(report, usd_limit=args.usd_limit)
        print(
            json.dumps(report, sort_keys=True), flush=True
        )  # script output, not framework logging
        if message is not None:
            notify_agents(args.notify_agent, message)
        if message is not None or args.poll_seconds is None:
            return
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
