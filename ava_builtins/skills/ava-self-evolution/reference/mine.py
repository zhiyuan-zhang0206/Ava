#!/usr/bin/env python3
"""Cluster a week's failed / fumbled runs by what acted in them.

Reads a dataset file written by `collect.py`, keeps only the records labeled
`failed` or `fumbled`, and groups them twice: by each skill in `skills_touched`
(records with no attributed skill land in an `(unattributed)` bucket), and by
each plugin contribution in `plugins_activated`. The output is a compact
markdown digest: one section per bucket, each listing its bad runs with the
concrete failure signals.

The plugin half exists because hooks and wraps used to act silently — a plugin
that rewrote state or short-circuited an SDK call in a bad run was invisible
here, so plugin-caused regressions could not be mined at all (issue #40). It
has no `(unattributed)` bucket: most runs legitimately have no plugin
activation, so the bucket would hold the whole week and say nothing.

This digest is what a deep-dive worker reads in step 3 of the weekly flow —
it points the worker at "these runs, this skill" instead of the whole dataset.
It makes no judgment of its own; the worker reads the real traces and decides.

Run standalone:

    .venv/bin/python skills/ava-self-evolution/reference/mine.py <dataset.jsonl>

With no path, it defaults to this week's dataset under `$AVA_HOME`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

UNATTRIBUTED = "(unattributed)"
BAD_LABELS = ("failed", "fumbled")


def load_records(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def cluster_by_skill(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket the bad runs under each skill they touched. A run touching two
    skills appears under both — attribution is a suspicion, not a partition."""
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        if rec["label"] not in BAD_LABELS:
            continue
        skills = rec["skills_touched"] or [UNATTRIBUTED]
        for skill in skills:
            clusters[skill].append(rec)
    return clusters


def cluster_by_plugin(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket the bad runs under each plugin contribution that fired in them.

    Keys are `plugins_activated`'s `"<plugin>/<surface>/<identifier>"` — the
    specific hook / wrap / prompt section, not merely the plugin, so a digest
    points at the thing to read. `.get` because datasets collected before the
    field existed simply have no plugin attribution (same tolerance
    `exec_duration_stats` shows for `exec_duration_s`)."""
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        if rec["label"] not in BAD_LABELS:
            continue
        for contribution in rec.get("plugins_activated", {}):
            clusters[contribution].append(rec)
    return clusters


def _oneline(text: str, limit: int = 140) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed[:limit] + ("..." if len(collapsed) > limit else "")


def _signals(rec: dict[str, Any]) -> str:
    bits = []
    if rec["breached"]:
        bits.append("breached")
    if rec["last_exec_failed"]:
        bits.append("ended-on-exec-fail")
    if rec["exec_failed"]:
        bits.append(f"exec_failed={rec['exec_failed']}")
    if rec["followup_prompts"]:
        bits.append(f"reprompts={len(rec['followup_prompts'])}")
    if rec["compactions"]:
        bits.append(f"compactions={rec['compactions']}")
    return ", ".join(bits) or "(no signal)"


def exec_duration_stats(records: list[dict[str, Any]]) -> str:
    """Identify runs with outlier code-execution duration."""
    durations = [(r, r.get("exec_duration_s", 0)) for r in records]
    non_zero = [(r, d) for r, d in durations if d > 0]
    if not non_zero:
        return ""
    sorted_durs = sorted(d for _, d in non_zero)
    p95 = sorted_durs[int(len(sorted_durs) * 0.95)]
    outliers = [(r, d) for r, d in non_zero if d >= p95 and d > 30]
    if not outliers:
        return ""
    lines = [
        f"## Duration outliers — exec_duration_s >= p95 ({p95:.1f}s)",
        f"({len(outliers)} runs above threshold, min reported >30s)",
        "",
    ]
    outliers.sort(key=lambda x: -x[1])
    for r, d in outliers[:20]:
        lines.append(
            f"- run #{r['agent_id']} [{r['label']}] "
            f"exec={d:.1f}s turns={r['turns']} "
            f'"{_oneline(r["task_prompt"], 80)}"'
        )
    lines.append("")
    return "\n".join(lines)


def render(clusters: dict[str, list[dict[str, Any]]], empty: str = "") -> str:
    if not clusters:
        return (empty or "No failed or fumbled runs this week.") + "\n"
    lines: list[str] = []
    for bucket, recs in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        failed = sum(1 for r in recs if r["label"] == "failed")
        fumbled = sum(1 for r in recs if r["label"] == "fumbled")
        lines.append(f"## {bucket} — {len(recs)} bad runs ({failed} failed, {fumbled} fumbled)")
        for r in recs:
            lines.append(
                f"- run #{r['agent_id']} [{r['label']}] "
                f'"{_oneline(r["task_prompt"])}"  ({_signals(r)})'
            )
        lines.append("")
    return "\n".join(lines)


def _default_dataset() -> Path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100, PTH120
    from collect import _default_week, dataset_path  # sibling script

    return dataset_path(_default_week())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cluster a dataset's failed/fumbled runs by skill (markdown digest).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "dataset", nargs="?", default=None, help="dataset JSONL path (default: this week's)"
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    path = Path(args.dataset) if args.dataset else _default_dataset()
    records = load_records(path)
    print(f"# Failure clusters — {path.name} ({len(records)} runs total)\n")
    print("# By skill\n")
    print(render(cluster_by_skill(records)))
    print("# By plugin contribution\n")
    print(
        render(
            cluster_by_plugin(records),
            empty="No plugin hook, wrap, or prompt section fired in a bad run this week.",
        )
    )
    dur_stats = exec_duration_stats(records)
    if dur_stats:
        print(dur_stats)


if __name__ == "__main__":
    main()
