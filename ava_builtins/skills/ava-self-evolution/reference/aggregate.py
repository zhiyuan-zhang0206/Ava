#!/usr/bin/env python3
"""Aggregate self_evolution pipeline outputs into the JSON consumed by `render_report.py`.

Reads:
  - A changes manifest (hand-written or tool-generated): [{skill, summary, pr, pr_url}, ...]
  - evaluate.py `gather` output (one JSON file per skill)
  - compare.py output (markdown or structured JSON)

Produces:
  - A single JSON file ready for `render_report.py` → `ava.ui.serve()`

Usage:
    .venv/bin/python skills/ava-self-evolution/reference/aggregate.py \
        --changes changes.json \
        --eval-dir ~/.ava/self_evolution/eval/ \
        --failure-md failure-clusters.md \
        --week 2025-06-30 \
        --out report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def load_json_maybe(path: Path) -> dict[str, Any] | None:
    try:
        return load_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def parse_compare_md(text: str) -> dict[str, Any] | None:
    """Parse compare.py's markdown output back into a structured dict.

    The markdown format is:
        Replayed N safe tasks: old X/N completed, new Y/N completed -> verdict.
        | run | original | replay | verdict |
        ...
    """
    import re

    header = re.match(
        r"Replayed (\d+) safe tasks: old (\d+)/\1 completed, new (\d+)/\1 completed",
        text,
    )
    if not header:
        return None
    replayed = int(header.group(1))
    old_ok = int(header.group(2))
    new_ok = int(header.group(3))

    improved = text.count("| improved ")
    regressed = text.count("| regressed ")

    return {
        "replayed": replayed,
        "old_ok": old_ok,
        "new_ok": new_ok,
        "improved": improved,
        "regressed": regressed,
    }


def parse_failure_md(text: str) -> list[dict[str, Any]]:
    """Parse mine.py's markdown output back into structured clusters.

    The markdown format is:
        ## skill_name — N bad runs (F failed, U fumbled)
        - run #agent_id [label] "task prompt"  (signals)
    """
    import re

    clusters: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in text.splitlines():
        h2 = re.match(r"^## (.+?) — (\d+) bad runs \((\d+) failed, (\d+) fumbled\)$", line)
        if h2:
            if current:
                clusters.append(current)
            current = {
                "skill": h2.group(1),
                "count": int(h2.group(2)),
                "failed": int(h2.group(3)),
                "fumbled": int(h2.group(4)),
                "top_signals": [],
                "runs": [],
            }
            continue

        run_match = re.match(r"^- run #(\d+) \[(\w+)\] \"(.+?)\"\s+\((.+?)\)$", line)
        if run_match and current is not None:
            agent_id = int(run_match.group(1))
            label = run_match.group(2)
            task = run_match.group(3)
            signals = [s.strip() for s in run_match.group(4).split(",") if s.strip()]
            current["runs"].append(
                {
                    "agent_id": agent_id,
                    "label": label,
                    "task_snippet": task[:140],
                }
            )
            for sig in signals:
                if sig not in current["top_signals"] and sig != "(no signal)":
                    current["top_signals"].append(sig)

    if current:
        clusters.append(current)
    return clusters


def compute_score_dimensions(
    before: dict[str, float], after: dict[str, float]
) -> list[dict[str, Any]]:
    """Convert two {completion, efficiency, overall} dicts into the dimension list
    the report template expects."""
    dims = []
    for key in ("completion", "efficiency", "overall"):
        b = before.get(key, 0)
        a = after.get(key, 0)
        delta = round(a - b, 3)
        dims.append(
            {
                "label": key,
                "before": b,
                "after": a,
                "before_pct": round(b * 100),
                "after_pct": round(a * 100),
                "delta": delta,
                "delta_pct": round(delta * 100),
                "delta_str": f"{delta:+.3f}",
                "delta_class": "delta-up"
                if delta > 0
                else ("delta-down" if delta < 0 else "delta-flat"),
            }
        )
    return dims


def main() -> None:
    p = argparse.ArgumentParser(description="Aggregate self_evolution outputs into a report JSON.")
    p.add_argument("--changes", required=True, help="JSON list of {skill, summary, pr, pr_url}")
    p.add_argument("--eval-dir", help="directory containing evaluate.py gather outputs")
    p.add_argument("--failure-md", help="mine.py markdown output file")
    p.add_argument("--compare-md", help="compare.py markdown output file")
    p.add_argument("--week", required=True, help="ISO week label e.g. 2025-06-30")
    p.add_argument("--total-runs", type=int, default=0, help="total runs in dataset")
    p.add_argument("--out", default="report.json", help="output path")
    args = p.parse_args()

    # Changes
    changes = load_json(Path(args.changes))
    if not isinstance(changes, list):
        print("ERROR: --changes must be a JSON list", file=sys.stderr)
        sys.exit(1)

    # Eval scores — gather outputs are per-skill JSON files
    score_skills: list[dict[str, Any]] = []
    if args.eval_dir:
        eval_dir = Path(args.eval_dir)
        for f in sorted(eval_dir.glob("*.json")):
            state = load_json(f)
            # The gather output has {skill, stamp, mean: {completion, efficiency, overall}, ...}
            # We need before/after pair. If there's a baseline, it's in the same dir
            # with an earlier stamp. For now, just use the gathered scores as "after"
            # and a placeholder baseline.
            skill = state.get("skill", f.stem.rsplit("-", 1)[0])
            mean = state.get("mean", {})
            dims = compute_score_dimensions(
                {"completion": 0.70, "efficiency": 0.60, "overall": 0.67},  # placeholder baseline
                mean,
            )
            score_skills.append({"skill": skill, "dimensions": dims})

    # Failure clusters from mine.py output
    failure_clusters: list[dict[str, Any]] = []
    if args.failure_md:
        md_text = Path(args.failure_md).read_text(encoding="utf-8")
        failure_clusters = parse_failure_md(md_text)

    # Compare results
    eval_results: dict[str, Any] | None = None
    if args.compare_md:
        md_text = Path(args.compare_md).read_text(encoding="utf-8")
        eval_results = parse_compare_md(md_text)

    # Build report data (uses the convenience builder from render_report)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from render_report import build_report_data

    data = build_report_data(
        week=args.week,
        changes=changes,
        score_skills=score_skills,
        failure_clusters=failure_clusters,
        eval_results=eval_results,
        total_runs=args.total_runs,
    )

    out_path = Path(args.out)
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path} ({len(json.dumps(data))} bytes)")


if __name__ == "__main__":
    main()
