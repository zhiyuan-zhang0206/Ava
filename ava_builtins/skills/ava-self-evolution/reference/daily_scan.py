#!/usr/bin/env python3
"""Daily incremental self-evolution scan.

Collects the past N days (default 1) of real agent runs into a trace
dataset — the same JSONL records the weekly collect builds — writes the
full report beside it (`<date>.report.txt`), prints a compact stdout view
ending with a summary + pointer to that report, and exits 2 (ALERT) when any
run is worth mining.

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
from typing import Any, NamedTuple

# PYTHONSAFEPATH=1 keeps the script's own directory off sys.path — restore
# it for the sibling import (the reference dir is a script dir, not a package).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100, PTH120
import collect  # sibling script, resolved via sys.path[0]
import mirror_backfill  # sibling script, resolved via sys.path[0]

from shared.paths import ava_home

ORCHESTRATION_SKILLS = ("ava-workflow", "ava-dynamic-workflow", "ava-goal")

# The runner delivers only the output tail (last 2000 chars); the fixed
# report scaffold around the bad list costs ~450-600 chars, so the rich list
# is capped here and a longer one compacts to counter lines once the full
# report is persisted and pointed to. At the 2026-09-12 scale (48 bad runs)
# the compact view measures ~1500 chars; past ~55 runs the head lines leave
# the tail, while the summary + pointer always land.
BAD_LIST_MAX_CHARS = 1400


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


def _compact_bad_line(rec: dict[str, Any]) -> str:
    """One bad run as counter tokens — the compact fallback for a bad list
    over BAD_LIST_MAX_CHARS (task text stays in the persisted full report).
    Tokens mirror _why(): rep=re-prompts, c=corrections, pf=peer feedback,
    ef=exec failures, last=last exec failed, cp=compactions, br=breach,
    term=terminated without output."""
    parts: list[str] = []
    if rec.get("followup_prompts"):
        parts.append(f"rep{len(rec['followup_prompts'])}")
    if rec.get("corrections"):
        parts.append(f"c{len(rec['corrections'])}")
    if rec.get("peer_feedback"):
        parts.append(f"pf{len(rec['peer_feedback'])}")
    if rec.get("exec_failed"):
        parts.append(f"ef{rec['exec_failed']}")
    if rec.get("last_exec_failed"):
        parts.append("last")
    if rec.get("compactions"):
        parts.append(f"cp{rec['compactions']}")
    if rec.get("breached"):
        parts.append("br")
    if (
        rec.get("terminated")
        and not rec.get("final_output", "").strip()
        and rec.get("turns", 0) > 0
    ):
        parts.append("term")
    return f"  #{rec['agent_id']} {rec['label']} " + (" ".join(parts) if parts else "-")


class ScanResult(NamedTuple):
    """One scan's output, carrying its own provenance.

    `source_note` is None on the normal (gateway) path and names the fallback
    source otherwise — it must travel with the records so the report and the
    schedule's wake message cannot present mirror data as the live read.
    `missing_days` names mirror days absent from a fallback window (empty
    otherwise); a non-empty value forces the ALERT exit — a partial window
    must not read as "nothing to act on".
    """

    records: list[dict[str, Any]]
    path: Path
    counts: dict[str, int]
    source_note: str | None = None
    missing_days: tuple[str, ...] = ()


def scan(days: int, week: str | None = None, *, include_test: bool = False) -> ScanResult:
    """Collect the window's runs and write them under
    `$AVA_HOME/self_evolution/daily/<date>.jsonl`. The weekly `dataset/`
    files are never touched, so a daily run can never clobber a weekly
    file that shares the same date stamp.

    `counts` reports how many window agents the source produced and how many
    the TEST- filter dropped (`{"seen", "excluded_test", "skipped_meta"}`
    from collect.collect_with_counts) — the empty-dataset sentinel keys on
    it to tell a TEST-only window from a broken data source.

    Source: the gateway's /api/events (Loki-backed) normally. When the
    gateway refuses with the no-observability code — a cluster without an
    observability stack, a policy state rather than an outage — the window
    is collected from the local event mirror instead (mirror_backfill,
    loudly marked in `source_note`); a transient /api/events failure still
    fails the scan, as before."""
    week = week or datetime.now(UTC).date().isoformat()
    source_note: str | None = None
    missing_days: list[str] = []
    try:
        records, counts = collect.collect_with_counts(days, week, include_test=include_test)
    except collect.ObservabilityReadUnavailable as exc:
        print(
            f"[{datetime.now(UTC).isoformat()}] gateway observability reads unavailable "
            f"({exc}) — collecting from the local event mirror"
        )
        records, counts, missing_days = mirror_backfill.collect_from_mirror(
            days, week, include_test=include_test
        )
        source_note = "local event mirror (no observability on this cluster)"
        if missing_days:
            source_note += (
                f"; mirror file missing for {', '.join(missing_days)} — window may be partial"
            )
    out_dir = ava_home() / "self_evolution" / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{week}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return ScanResult(records, path, counts, source_note, tuple(missing_days))


def _is_test_only_window(records: list[dict[str, Any]], counts: dict[str, int] | None) -> bool:
    """True when the window is empty only because the TEST- filter removed
    every run the source produced — a quiet day, not an outage."""
    if records:
        return False
    return bool(counts and counts["seen"] > 0 and counts["excluded_test"] == counts["seen"])


def alert_exit(
    records: list[dict[str, Any]],
    counts: dict[str, int] | None = None,
    *,
    missing_days: tuple[str, ...] = (),
) -> int:
    """0 when nothing is worth acting on, 2 (ALERT) when any run is bad —
    or when no runs were collected at all: an empty dataset means the data
    source is broken, which must never report as "nothing to act on"
    (2026-08-14: PG events froze and the scan green-lit an empty day).

    `counts` (collect.collect_with_counts) refines the sentinel: an empty
    dataset whose every pre-filter run was a TEST- spawn is the filter
    working as designed, not a source outage, and exits 0 (QA review of
    PR #698, 2026-08-29). Without counts the legacy sentinel applies.

    `missing_days` (a fallback window whose mirror file(s) are absent)
    alerts regardless of the runs the window did produce: the gap itself is
    an unexplained window that could hide bad runs (mirror_backfill's iron
    rule — a partial window must not silently become a partial dataset)."""
    if missing_days:
        return 2
    if not records:
        return 0 if _is_test_only_window(records, counts) else 2
    return 2 if any(r["label"] != "ok" for r in records) else 0


def render(
    records: list[dict[str, Any]],
    path: Path,
    days: int,
    counts: dict[str, int] | None = None,
    *,
    report_path: Path | None = None,
    compact_bad: bool = False,
    source_note: str | None = None,
) -> str:
    """Render the scan report. With `report_path` set the output closes with
    a summary line and a pointer to the persisted full report. `compact_bad`
    may replace an oversized bad list (> BAD_LIST_MAX_CHARS chars) with one
    counter line per run, but only when `report_path` is set: without a
    stored report the rich lines are the only copy of the details.
    `source_note` (fallback provenance) lands just before the closing block —
    the stable tail — so the schedule's crop cannot sever it."""
    counts_by_label = Counter(r["label"] for r in records)
    skill_counts = Counter(
        skill for r in records if "skills_touched" in r for skill in r["skills_touched"]
    )
    corrections = sum(len(r["corrections"]) for r in records)
    peer = sum(len(r["peer_feedback"]) for r in records)
    breached = sum(1 for r in records if r["breached"])
    execfail = sum(1 for r in records if r["exec_failed"])
    builtin_help_calls = sum(r.get("builtin_help_calls", 0) for r in records)
    builtin_help_agents = sum(bool(r.get("builtin_help_calls", 0)) for r in records)
    builtin_help_on_ava = sum(r.get("builtin_help_on_ava", 0) for r in records)
    subprocess_calls = sum(r.get("subprocess_calls", 0) for r in records)
    shell_run_calls = sum(
        r["tools_called"].get("ava.shell.run", 0) for r in records if "tools_called" in r
    )
    subprocess_line = f"subprocess calls: {subprocess_calls}"
    if any("tools_called" in r for r in records):
        subprocess_line += f" (shell.run {shell_run_calls})"
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
        f"builtin help(): {builtin_help_calls} calls across {builtin_help_agents} agents "
        f"({builtin_help_on_ava} on ava.* targets)",
        subprocess_line,
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
        sorted_bad = sorted(bad, key=lambda r: r["agent_id"])
        rich_lines: list[str] = []
        for rec in sorted_bad:
            task = (rec["task_prompt"] or "").strip().replace("\n", " ")
            if len(task) > 120:
                task = task[:117] + "..."
            line = f"  #{rec['agent_id']} {rec['label']} — {', '.join(_why(rec))}"
            if task:
                line += f" | task: {task}"
            rich_lines.append(line)
        head = f"ALERT — {len(bad)} run(s) worth mining:"
        # Compaction needs a stored report: the closing pointer keeps the
        # dropped task text one hop away and survives tail truncation.
        if (
            compact_bad
            and report_path is not None
            and sum(len(line) for line in rich_lines) > BAD_LIST_MAX_CHARS
        ):
            lines.append(f"{head} (compact; details in report)")
            lines.extend(_compact_bad_line(rec) for rec in sorted_bad)
        else:
            lines.append(head)
            lines.extend(rich_lines)
    if source_note:
        lines.append(f"source: {source_note}")
    if report_path is not None:
        lines.append(
            f"summary: {len(records)} runs (ok {counts_by_label['ok']} / "
            f"fumbled {counts_by_label['fumbled']} / failed {counts_by_label['failed']})"
            f" | corrections {corrections} | peer {peer} | breached {breached} "
            f"| exec-fail runs {execfail}"
        )
        lines.append(f"full report: {report_path}")
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
    result = scan(args.days, include_test=args.include_test)
    full = render(
        result.records, result.path, args.days, result.counts, source_note=result.source_note
    )
    report_path = result.path.with_suffix(".report.txt")
    try:
        # Publish atomically (tmp + rename): a reader never sees a partial
        # report, and a failed rerun leaves the previous file in place.
        tmp_path = report_path.with_name(report_path.name + ".tmp")
        tmp_path.write_text(full, encoding="utf-8")
        tmp_path.replace(report_path)
    except OSError as exc:
        print(f"warning: could not write full report {report_path}: {exc}", file=sys.stderr)
        report_path = None
    out = render(
        result.records,
        result.path,
        args.days,
        result.counts,
        report_path=report_path,
        compact_bad=True,
        source_note=result.source_note,
    )
    print(out)
    raise SystemExit(alert_exit(result.records, result.counts, missing_days=result.missing_days))


if __name__ == "__main__":
    main()
