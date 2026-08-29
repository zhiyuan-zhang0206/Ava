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

2026-08-30 (Task #1995): GC-storm fix for dense windows. backfill() used to
hold every in-window row of every day in memory as parsed dicts until
collect() ran (08-24: ~1.9GB peak, ~900MB/day of allocation churn, gen0
collections x35). It now works in two phases:
  Phase 1 streams each day file once — rows outside the window or outside the
  two collected categories are dropped before the timestamp is even parsed,
  and kept rows are written as their ORIGINAL line bytes to per-category temp
  files plus a (ts, offset) sort index. Phase 1 retains only that index —
  O(kept rows) at ~157B/row — instead of the full parsed row dicts (~3.5KB/row)
  the old path held, so a multi-day window no longer accumulates its rows
  (chunked release; the row payload itself is O(1) per day).
  Phase 2 installs a consumer that streams a category's temp file in ts order
  (the same deterministic order the in-memory sort produced) and yields row
  dicts one at a time, so a row's dict dies as soon as collect groups it into
  (event_name, attributes). Dedup and agent filtering are byte-for-byte the
  same semantics as before.
  The cyclic GC is disabled for the whole backfill: every object this
  pipeline builds (parsed rows, tuples, records) is acyclic, so refcounting
  frees everything and the collector only added storm (Task #1995 measured
  gen0 collections 178 -> ~6200 and multi-hundred-MB RSS swings in one dense
  run). One gc.collect() sweeps any library-created cycles before the CLI
  exits.

Standalone:  python mirror_backfill.py <days> [week]
"""

from __future__ import annotations

import gc
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

# PYTHONSAFEPATH=1 keeps the script's own directory off sys.path — restore
# it for the sibling import (the reference dir is a script dir, not a package).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100, PTH120
import collect

from shared.paths import ava_home

MIRROR_DIR = ava_home() / "logs"

# Categories the collect pipeline consumes. Every other category in the
# mirror (log rows carry exec stdout payloads — the largest rows) is dropped
# before timestamp parsing.
_KEPT_CATEGORIES = ("telemetry", "audit")

# Category -> [(ts string, byte offset)] in file order, built by backfill()
# phase 1. The consumer sorts it by ts and streams the temp file through the
# offsets, so rows never need to be held in memory to be ordered.
SortIndex = dict[str, list[tuple[str, int]]]


def _mirror_fetch_factory(
    rows: dict[str, list[dict]],
) -> Callable[..., list[dict]]:
    """Build the in-memory consumer backfill() used before Task #1995, and the
    one the unit tests drive directly: category lookup + optional agent
    filter, then id-based dedup against true duplicates. Rows are expected
    pre-sorted by ts (backfill() sorted them; the temp-file consumer sorts
    itself).

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


def _mirror_fetch_factory_from_dir(tmpdir: Path, index: SortIndex) -> Callable[..., Iterator[dict]]:
    """Build the disk-backed consumer backfill() installs since Task #1995.

    The temp dir holds one file per kept category with the window rows as
    their original line bytes; `index` maps category -> [(ts, byte offset)]
    in file order. The consumer streams a category's file in ts order (stable
    sort over the index — the same deterministic order the in-memory path's
    sort produced) and YIELDS row dicts one at a time: a row's dict dies as
    soon as collect groups it, instead of the whole category living in memory
    at once (the 08-24 GC-storm shape).

    Dedup and agent-filter semantics are identical to _mirror_fetch_factory:
    rows with an id dedupe first-occurrence-wins, None-id rows pass through,
    the agent filter applies before dedup, and _from/_to are unused by
    contract (the rows are pre-filtered by the window).
    """

    def mirror_fetch(
        category: str,
        _from: datetime | None,
        _to: datetime | None,
        agent_id: int | None = None,
    ) -> Iterator[dict]:
        mf = tmpdir / f"{category}.jsonl"
        idx = index.get(category)
        if idx is None or not mf.exists():
            return
        seen: set[int] = set()
        with mf.open() as fh:
            for _ts, offset in sorted(idx, key=lambda item: item[0]):
                fh.seek(offset)
                r = json.loads(fh.readline())
                if agent_id is not None and r.get("agent_id") != agent_id:
                    continue
                rid = r.get("id")
                if rid is not None:
                    if rid in seen:
                        continue
                    seen.add(rid)
                yield r

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
    local = datetime.now().astimezone().tzinfo
    missing_days: list[str] = []
    per_day: dict[str, int] = {}
    daily_dir = ava_home() / "self_evolution" / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    # A SIGKILLed run leaves its staging dir behind (nothing can clean up
    # after SIGKILL); sweep orphans older than an hour so a crashed dense
    # backfill does not leak hundreds of MB of staged rows. A live run's
    # dir is younger than that (QA #1010 nit).
    for stale in daily_dir.glob("mirror-backfill-*"):
        try:
            if time.time() - stale.stat().st_mtime > 3600:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            pass
    tmp = tempfile.TemporaryDirectory(prefix="mirror-backfill-", dir=daily_dir)
    tmpdir = Path(tmp.name)
    writers: dict[str, TextIO] = {}
    counts: dict[str, int] = dict.fromkeys(_KEPT_CATEGORIES, 0)
    index: SortIndex = {cat: [] for cat in _KEPT_CATEGORIES}
    gc_was_enabled = gc.isenabled()
    if gc_was_enabled:
        # Every object this pipeline builds is acyclic (parsed rows, tuples,
        # records) — refcounting frees it, and the cyclic collector only
        # added storm (Task #1995: gen0 collections 178 -> ~6200 in one dense
        # run, multi-hundred-MB RSS swings). One sweep at the end collects
        # anything a library created with cycles.
        gc.disable()
    try:
        # Phase 1 — stream each day file once. Rows outside the window or
        # outside the kept categories are dropped before the timestamp is
        # parsed; kept rows are written as their ORIGINAL line bytes to a
        # per-category temp file plus a (ts, offset) index entry. Nothing is
        # retained across days, so a multi-day window no longer accumulates
        # every day's rows in memory (chunked release).
        for cat in _KEPT_CATEGORIES:
            writers[cat] = (tmpdir / f"{cat}.jsonl").open("w")
        try:
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
                        cat = ev.get("category")
                        if cat not in writers:
                            continue  # log & friends are never collected
                        ts = datetime.fromisoformat(ev["ts"])
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=local)
                        if not (window_from <= ts < window_to):
                            continue
                        counts[cat] += 1
                        day_rows += 1
                        index[cat].append((ev["ts"], writers[cat].tell()))
                        writers[cat].write(line)
                per_day[day] = day_rows
                d += timedelta(days=1)
        finally:
            for writer in writers.values():
                writer.close()
        for day, n in sorted(per_day.items()):
            print(f"[mirror] {day}: {n} rows in window")
        n_telem = counts["telemetry"]
        n_audit = counts["audit"]
        print(f"[mirror] telemetry={n_telem} audit={n_audit} rows in window")
        if missing_days:
            print(
                "warning: mirror file missing for "
                + ", ".join(missing_days)
                + " — dataset is partial"
            )
        # Phase 2 — the consumer streams the staged rows in ts order while
        # collect() groups and builds records; the temp dir lives until the
        # output is written. The replacement is restored afterwards: a
        # same-process caller that collects again must go back to the Loki
        # path, not silently read a consumed temp dir (QA #1010 nit).
        original_fetch = collect._fetch_events_window
        collect._fetch_events_window = _mirror_fetch_factory_from_dir(tmpdir, index)
        try:
            records = collect.collect(days, week, from_=window_from, to=window_to)
        finally:
            collect._fetch_events_window = original_fetch
        out_dir = ava_home() / "self_evolution" / "daily"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{week}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        counts_label = Counter(r["label"] for r in records)
        print(f"[mirror] wrote {path}: {len(records)} runs ({dict(counts_label)})")
        return path, missing_days
    finally:
        tmp.cleanup()  # temp rows are consumed — release the disk space
        if gc_was_enabled:
            gc.enable()
            gc.collect()  # one sweep for the run's churn, then the storm stops


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
