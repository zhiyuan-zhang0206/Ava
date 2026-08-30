#!/usr/bin/env python3
"""Daily incremental self-evolution scan.

Collects the past N days (default 1) of real agent runs into a trace
dataset — the same JSONL records the weekly collect builds — then prints a
compact daily report and exits 2 (ALERT) when any run is worth mining.

Exit codes:
    0   no bad runs — nothing to act on
    2   ALERT — failed/fumbled runs found, or an empty dataset that the
        TEST- filter cannot explain (data source outage); the schedule wakes
        the self-evolution agent with this report
    1   hard failure (bad args, DB unreachable) — the schedule logs it
        and wakes the agent to investigate

A window whose only activity was TEST- prefixed spawns exits 0: the filter
removed every run by design, which is a quiet day, not an outage (QA review
of PR #698, 2026-08-29). The empty-dataset sentinel keys on pre-filter
activity — the source produced runs, the filter just dropped them all — so
the two cases stay distinguishable.

The alert threshold is deliberately low (one bad run alerts): the scan
wakes the self-evolution agent, not the user, so a false positive costs
one cheap review, while a missed bad run loses the earliest signal.

Run it like its sibling collect.py (the reference dir must be on
sys.path — run as a script, not imported):

    .venv/bin/python skills/ava-self-evolution/reference/daily_scan.py --days 1
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# PYTHONSAFEPATH=1 keeps the script's own directory off sys.path — restore
# it for the sibling import (the reference dir is a script dir, not a package).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100, PTH120
import collect  # sibling script, resolved via sys.path[0]

from shared.paths import ava_home

ORCHESTRATION_SKILLS = ("ava-workflow", "ava-dynamic-workflow", "ava-goal")


def _why(rec: dict[str, Any]) -> list[str]:
    """Human-readable signals explaining a non-ok label. Display-only; the
    label itself remains label.py's contract."""
    why: list[str] = []
    if rec.get("corrections"):
        why.append(f"{len(rec['corrections'])} user correction(s)")
    if rec.get("peer_feedback"):
        why.append(f"{len(rec['peer_feedback'])} corrective peer message(s)")
    if rec.get("followup_prompts"):
        why.append(f"{len(rec['followup_prompts'])} follow-up re-prompt(s)")
    if rec.get("breached"):
        why.append("delivery breach")
    if rec.get("exec_failed"):
        why.append(f"{rec['exec_failed']} failed exec(s)")
    if rec.get("compactions"):
        why.append(f"{rec['compactions']} compaction(s)")
    if rec.get("last_exec_failed"):
        why.append("last exec failed")
    if (
        rec.get("terminated")
        and not rec.get("final_output", "").strip()
        and rec.get("turns", 0) > 0
    ):
        why.append("terminated without output")
    return why or ["no explicit signal"]


def scan(
    days: int, week: str | None = None, *, include_test: bool = False
) -> tuple[list[dict[str, Any]], Path, dict[str, int]]:
    """Collect the window's runs and write them under
    `$AVA_HOME/self_evolution/daily/<date>.jsonl`. The weekly `dataset/`
    files are never touched, so a daily run can never clobber a weekly
    file that shares the same date stamp.

    Returns (records, path, counts): `counts` reports how many window
    agents the source produced and how many the TEST- filter dropped
    (`{"seen", "excluded_test", "skipped_meta"}` from
    collect.collect_with_counts) — the empty-dataset sentinel keys on it
    to tell a TEST-only window from a broken data source."""
    week = week or datetime.now(UTC).date().isoformat()
    records, counts = collect.collect_with_counts(days, week, include_test=include_test)
    out_dir = ava_home() / "self_evolution" / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{week}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return records, path, counts


def _is_test_only_window(records: list[dict[str, Any]], counts: dict[str, int] | None) -> bool:
    """True when the window is empty only because the TEST- filter removed
    every run the source produced — a quiet day, not an outage."""
    if records:
        return False
    return bool(counts and counts["seen"] > 0 and counts["excluded_test"] == counts["seen"])


def alert_exit(records: list[dict[str, Any]], counts: dict[str, int] | None = None) -> int:
    """0 when nothing is worth acting on, 2 (ALERT) when any run is bad —
    or when no runs were collected at all: an empty dataset means the data
    source is broken, which must never report as "nothing to act on"
    (2026-08-14: PG events froze and the scan green-lit an empty day).

    `counts` (collect.collect_with_counts) refines the sentinel: an empty
    dataset whose every pre-filter run was a TEST- spawn is the filter
    working as designed, not a source outage, and exits 0 (QA review of
    PR #698, 2026-08-29). Without counts the legacy sentinel applies."""
    if not records:
        return 0 if _is_test_only_window(records, counts) else 2
    return 2 if any(r["label"] != "ok" for r in records) else 0


def render(
    records: list[dict[str, Any]], path: Path, days: int, counts: dict[str, int] | None = None
) -> str:
    counts_by_label = Counter(r["label"] for r in records)
    skill_counts = Counter(
        skill for r in records if "skills_touched" in r for skill in r["skills_touched"]
    )
    corrections = sum(len(r["corrections"]) for r in records)
    peer = sum(len(r["peer_feedback"]) for r in records)
    breached = sum(1 for r in records if r["breached"])
    execfail = sum(1 for r in records if r["exec_failed"])
    top_skills = (
        ", ".join(f"{skill} {count}" for skill, count in skill_counts.most_common(5)) or "none"
    )
    orchestration_counts = ", ".join(
        f"{skill} {skill_counts[skill]}" for skill in ORCHESTRATION_SKILLS
    )
    lines = [
        f"self-evolution daily scan — {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"window: {days} day(s) | runs: {len(records)} (ok {counts_by_label['ok']} / fumbled {counts_by_label['fumbled']} / failed {counts_by_label['failed']})"
        f" | corrections {corrections} | peer feedback {peer} | breached {breached} | exec-fail runs {execfail}",
        f"dataset: {path}",
        "skills loaded:",
        f"  top 5: {top_skills}",
        f"  orchestration: {orchestration_counts}",
    ]
    if not records:
        if _is_test_only_window(records, counts):
            lines.append(
                "0 production runs — all "
                f"{counts['excluded_test']} window agent(s) were TEST- spawns "
                "(excluded by design); nothing to act on"
            )
        else:
            lines.append("ALERT — 0 runs collected: data source outage or collector failure")
    bad = [r for r in records if r["label"] != "ok"]
    if bad:
        lines.append(f"ALERT — {len(bad)} run(s) worth mining:")
        for rec in sorted(bad, key=lambda r: r["agent_id"]):
            task = (rec["task_prompt"] or "").strip().replace("\n", " ")
            if len(task) > 120:
                task = task[:117] + "..."
            line = f"  #{rec['agent_id']} {rec['label']} — {', '.join(_why(rec))}"
            if task:
                line += f" | task: {task}"
            lines.append(line)
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Daily incremental self-evolution scan — collect the past day's runs, "
        "report, alert (exit 2) on bad runs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--days", type=int, default=1, help="window size in days (default 1)")
    p.add_argument(
        "--include-test",
        action="store_true",
        help="include TEST- prefixed benchmark/probe spawns (measurement only; default excludes)",
    )
    args = p.parse_args()
    if args.days < 1:
        print("error: --days must be >= 1", file=sys.stderr)
        raise SystemExit(1)
    records, path, counts = scan(args.days, include_test=args.include_test)
    print(render(records, path, args.days, counts))
    raise SystemExit(alert_exit(records, counts))


if __name__ == "__main__":
    main()
