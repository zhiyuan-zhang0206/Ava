#!/usr/bin/env python3
"""Compare replayed outcomes against the dataset's recorded originals.

Reads the JSON that `replay.py --run` writes (each entry already carries the
original label from the dataset and the replay outcome under the current tree)
and renders a plain-count markdown block for the weekly report. No statistics,
no scoring — just "old K/N completed, new M/N completed, R regressed".

`ok` here means the run completed without a hard failure: the original label
was not `failed`, or the replay produced output without erroring. A task counts
as **regressed** when it used to complete and no longer does — the signal that
a proposed change made things worse.

Run:

    .venv/bin/python skills/ava-self-evolution/reference/compare.py <replay.json>

With no path it defaults to this week's replay file under `$AVA_HOME`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_outcomes(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _original_ok(outcome: dict[str, Any]) -> bool:
    return outcome["original_label"] != "failed"


def verdict(outcome: dict[str, Any]) -> str:
    old_ok, new_ok = _original_ok(outcome), outcome["replay_ok"]
    if old_ok and not new_ok:
        return "regressed"
    if new_ok and not old_ok:
        return "improved"
    return "same"


def render(outcomes: list[dict[str, Any]]) -> str:
    if not outcomes:
        return "No replay-safe tasks were replayed.\n"
    old_ok = sum(1 for o in outcomes if _original_ok(o))
    new_ok = sum(1 for o in outcomes if o["replay_ok"])
    verdicts = [verdict(o) for o in outcomes]
    regressed = verdicts.count("regressed")
    improved = verdicts.count("improved")

    if new_ok < old_ok:
        headline = f"proposal looks WORSE ({regressed} regressed)"
    elif new_ok > old_ok:
        headline = f"proposal looks better ({improved} improved)"
    else:
        headline = "no net change"

    lines = [
        f"Replayed {len(outcomes)} safe tasks: "
        f"old {old_ok}/{len(outcomes)} completed, new {new_ok}/{len(outcomes)} completed "
        f"-> {headline}.",
        "",
        "| run | original | replay | verdict |",
        "|-----|----------|--------|---------|",
    ]
    for o, v in zip(outcomes, verdicts, strict=True):
        replay_cell = "ok" if o["replay_ok"] else (o["replay_error"] or "no output")
        lines.append(f"| #{o['agent_id']} | {o['original_label']} | {replay_cell} | {v} |")
    return "\n".join(lines) + "\n"


def _default_replay_json() -> Path:
    from collect import _default_week  # sibling script

    from shared.paths import ava_home

    return ava_home() / "self_evolution" / "replay" / f"{_default_week()}.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Render a markdown old-vs-new comparison from a replay outcomes file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("replay", nargs="?", default=None, help="replay JSON (default: this week's)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    path = Path(args.replay) if args.replay else _default_replay_json()
    print(render(load_outcomes(path)))


if __name__ == "__main__":
    main()
