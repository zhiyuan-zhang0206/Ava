"""mirror_backfill — Loki dense-window 500 fallback for the daily scan.

2026-08-20: the gateway /api/events 500s on dense windows. The local event
mirror (logs/events-<date>.jsonl, local dates) is more complete than Loki for
these windows (parity gap direction verified 08-15), so a failed Loki collect
falls back to mirror rows through the same collect() pipeline.

2026-08-21: collect is imported as a sibling (this script lives in the
skill's reference dir), so the fallback runs the exact pipeline the daily
schedule runs. Output mirrors daily_scan.py: daily/<week>.jsonl with the
UTC-date week label.

2026-08-26 (Task #1408): the consumer dedupes mirror rows by the surrogate
event id (PR #356, shared/telemetry.event_id) — the emitter can append the
same event twice, and on 08-24/25 ~7% of mirror rows were byte-identical
duplicates that inflated backfilled datasets. Rows without an id (pre-#356
files) pass through, matching collect._fetch_events_window's None-id handling.
This skill package is the single source of truth; the fallback runs from the
installed copy ($AVA_HOME/skills/ava-self-evolution/reference/). A missing
mirror day is never silent: backfill() returns the missing days, prints a
warning and per-day row counts, and the CLI exits non-zero — a partial
window must not silently become a partial dataset (collect's iron rule).

Standalone:  python mirror_backfill.py <days> [week]
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

# PYTHONSAFEPATH=1 keeps the script's own directory off sys.path — restore
# it for the sibling import (the reference dir is a script dir, not a package).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100, PTH120
import collect

from shared.paths import ava_home

MIRROR_DIR = ava_home() / "logs"


def _mirror_fetch_factory(
    rows: dict[str, list[dict]],
) -> Callable[..., list[dict]]:
    """Build the consumer the backfill installs in place of
    collect._fetch_events_window: category lookup + optional agent filter,
    then id-based dedup against true duplicates.

    Mirrors the Loki consumer's semantics exactly: rows with an id are
    deduped on it (first occurrence wins, order preserved); rows without an
    id (mirror files written before PR #356) pass through — there is no key
    to dedupe on. _from/_to are part of the replaced function's contract and
    deliberately unused (the mirror rows are pre-filtered by the window).
    """

    def mirror_fetch(
        category: str,
        _from: datetime | None,
        _to: datetime | None,
        agent_id: int | None = None,
    ) -> list[dict]:
        out = rows.get(category, [])
        if agent_id is not None:
            out = [r for r in out if r.get("agent_id") == agent_id]
        seen: set[int] = set()
        deduped: list[dict] = []
        for r in out:
            rid = r.get("id")
            if rid is not None:
                if rid in seen:
                    continue
                seen.add(rid)
            deduped.append(r)
        return deduped

    return mirror_fetch


def backfill(days: int, week: str | None = None) -> tuple[Path, list[str]]:
    """Collect [now - days, now) from the local mirror, write daily/<week>.jsonl.

    Returns (path, missing_days); missing_days names the window days whose
    mirror file does not exist. A missing day is never silent: it is listed
    in the warning and the caller must treat the dataset as partial (the CLI
    exits non-zero). Per-day in-window row counts are always printed so a
    thin or absent day is visible in the log.
    """
    week = week or datetime.now(UTC).date().isoformat()
    window_to = datetime.now(UTC)
    window_from = window_to - timedelta(days=days)
    rows: dict[str, list[dict]] = {"telemetry": [], "audit": []}
    local = datetime.now().astimezone().tzinfo
    missing_days: list[str] = []
    per_day: dict[str, int] = {}
    d = (window_from - timedelta(minutes=1)).date()
    end_date = window_to.date()
    while d <= end_date:
        day = d.strftime("%Y%m%d")
        mf = MIRROR_DIR / f"events-{day}.jsonl"
        if not mf.exists():
            missing_days.append(day)
            d += timedelta(days=1)
            continue
        day_rows = 0
        with mf.open() as fh:
            for line in fh:
                if not line.strip():
                    continue
                ev = json.loads(line)
                ts = datetime.fromisoformat(ev["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=local)
                if not (window_from <= ts < window_to):
                    continue
                cat = ev.get("category")
                if cat in rows:
                    rows[cat].append(ev)
                    day_rows += 1
        per_day[day] = day_rows
        d += timedelta(days=1)
    for day, n in sorted(per_day.items()):
        print(f"[mirror] {day}: {n} rows in window")
    if missing_days:
        print(
            "warning: mirror file missing for " + ", ".join(missing_days) + " — dataset is partial"
        )
    for _cat, cat_rows in rows.items():
        cat_rows.sort(key=lambda r: r.get("ts", ""))
    print(f"[mirror] telemetry={len(rows['telemetry'])} audit={len(rows['audit'])} rows in window")
    collect._fetch_events_window = _mirror_fetch_factory(rows)
    records = collect.collect(days, week, from_=window_from, to=window_to)
    out_dir = ava_home() / "self_evolution" / "daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{week}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    counts = Counter(r["label"] for r in records)
    print(f"[mirror] wrote {path}: {len(records)} runs ({dict(counts)})")
    return path, missing_days


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python mirror_backfill.py <days> [week]", file=sys.stderr)
        raise SystemExit(2)
    days = int(sys.argv[1])
    week = sys.argv[2] if len(sys.argv) > 2 else None
    _path, missing_days = backfill(days, week)
    # Partial window is not success: a caller that watches the exit code
    # (schedule, handoff script) must see the gap.
    raise SystemExit(1 if missing_days else 0)


if __name__ == "__main__":
    main()
