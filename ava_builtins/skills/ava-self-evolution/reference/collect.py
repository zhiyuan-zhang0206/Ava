#!/usr/bin/env python3
"""Harvest the past week's real agent runs into a trace dataset file.

One JSON record per agent that was active in the window, written as JSONL to
`$AVA_HOME/self_evolution/dataset/<week>.jsonl`. Each record is assembled from
existing tables only (no new schema): the unified `events` stream —
`category=telemetry/log` carries token / turn / exec signals, `category=audit`
carries compaction, delivery breach, and the future
`skill_invoked` hard signal), `inbound_messages` (the user's task prompt and
any follow-up corrections), `agents_meta` (spawner, terminal status, final
message), and the stored checkpoint (the full transcript reconstructed across
compaction boundaries retained by Task #1125; only duplicate system prompts
are removed, fixing the 2026-08-10 truncation without hiding lived messages).

The dataset is the durable asset this whole skill iterates on: real tasks,
real traces, real outcomes. It lives under `$AVA_HOME` (per-deployment,
private) so the skill package itself stays publishable.

Run it standalone (the skill directory name has a hyphen, so these files are
scripts, not an importable package):

    .venv/bin/python skills/ava-self-evolution/reference/collect.py --days 7

`collect.py` imports its sibling `record.py`; run it as a script so the
reference directory is on `sys.path`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg

# PYTHONSAFEPATH=1 keeps the script's own directory off sys.path — restore
# it for the sibling import (the reference dir is a script dir, not a package).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # noqa: PTH100, PTH120
from record import LeakPaths, _plugins_activated, _transcript, build_record  # noqa: F401

from shared.config import settings
from shared.db import connect
from shared.paths import ava_home

# Task-origin inbound sources — capture every real task prompt, whether from
# the user ("user") or a spawner / peer agent ("agent:<id>"). System and
# watcher messages are deliberately excluded (nudges, heartbeats, wake-ups).
TASK_SOURCES = ["user"]  # plus "agent:%" matched via LIKE in the query

_BUILTIN_HELP_CALL_RE = re.compile(r"(?<![a-zA-Z0-9_.])help\s*\(")
_BUILTIN_HELP_ON_AVA_RE = re.compile(r"(?<![a-zA-Z0-9_.])help\s*\(\s*ava\.")
_SUBPROCESS_CALL_RE = re.compile(r"subprocess\.(run|Popen|check_output|check_call|call)\b")


# ─────── Loki-backed event fetches (paged via the gateway /api/events) ───────


def _gateway_headers() -> dict[str, str]:
    """Bearer auth from $AVA_HOME/.env when the cluster has a secret set."""
    env_file = Path(ava_home()) / ".env"
    if not env_file.exists():
        return {}
    for line in env_file.read_text().splitlines():
        if line.startswith("AVA_CLUSTER_SECRET="):
            secret = line.split("=", 1)[1].strip().strip("\"'")
            if secret:
                return {"Authorization": f"Bearer {secret}"}
    return {}


def _gateway_url() -> str:
    return settings.gateway.gateway_url or "http://localhost:8000"


def _events_page(
    client: httpx.Client,
    *,
    category: str,
    from_: datetime,
    to: datetime,
    agent_id: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """One /api/events page (offset always 0). Returns (items, meta)."""
    params: dict[str, Any] = {
        "category": category,
        "from": from_.isoformat(),
        "to": to.isoformat(),
        "limit": _PAGE,
        "offset": 0,
    }
    if agent_id is not None:
        params["agent_id"] = agent_id
    last_exc: Exception | None = None
    for attempt in range(_RETRIES + 1):
        try:
            resp = client.get(
                _gateway_url() + _EVENTS_PATH,
                params=params,
                headers=_gateway_headers(),
                timeout=_HTTP_TIMEOUT_S,
            )
            resp.raise_for_status()
            payload = resp.json()
            return payload.get("items", []), payload.get("meta", {})
        except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < _RETRIES:
                time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _fetch_events_window(
    category: str, from_: datetime, to: datetime, agent_id: int | None = None
) -> list[dict[str, Any]]:
    """All rows for (category, [from_, to)) via /api/events, oldest-first.

    Offset-free by construction: Loki offset paging is both O(n^2) and
    timeout-prone (gateway's 60s Loki budget — 2026-08-14 observed), so
    every request is offset=0 with limit=_PAGE, and any slice whose
    `meta.has_more` says the page overflowed is bisected in time until each
    half fits one page. Requests are count-free by design — `meta.total` is
    opt-in (with_total=1) and its count aggregation is exactly the Loki load
    the gateway shed (2026-08-18 change) — so this function reads `has_more`
    only, and a missing `has_more` raises instead of silently truncating. Rows are deduped by surrogate row id across slice boundaries (the
    API window is inclusive on both ends) and sorted by `ts` (the API
    returns newest-first). Raises on persistent HTTP errors — a partial
    window must not silently become a partial dataset.
    """
    out: list[dict[str, Any]] = []
    seen: set[int] = set()

    with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:

        def rec(start: datetime, end: datetime) -> None:
            if start >= end:
                return
            batch, meta = _events_page(
                client, category=category, from_=start, to=end, agent_id=agent_id
            )
            # has_more is a required, non-nullable EventsMeta field — index it,
            # not .get(): a silent False here would accept an oversized slice
            # without bisection and quietly truncate the dataset (the exact
            # quiet-failure class the 2026-08-14 outage was). KeyError = loud.
            has_more = meta["has_more"]
            if not batch and not has_more:
                return  # empty slice
            if not has_more or end - start <= _MIN_SLICE:
                if has_more:
                    print(
                        f"warning: {category} slice [{start}, {end}] has more than "
                        f"{_PAGE} rows in under {_MIN_SLICE} — tail may be truncated"
                    )
                for r in batch:
                    rid = r.get("id")
                    if rid is not None and rid in seen:
                        continue
                    if rid is not None:
                        seen.add(rid)
                    out.append(r)
                return
            mid = start + (end - start) / 2
            rec(start, mid)
            rec(mid, end)

        rec(from_, to)

    out.sort(key=lambda r: str(r.get("ts", "")))
    return out


def _group_by_agent(
    rows: list[dict[str, Any]],
) -> dict[int, list[tuple[str, dict[str, Any]]]]:
    """Group EventRow dicts into {agent_id: [(event_name, attributes)]};
    service-level rows (agent_id None) are dropped — the old PG contract."""
    out: dict[int, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for r in rows:
        agent_id = r.get("agent_id")
        if agent_id is None:
            continue
        out[agent_id].append((r["event_name"], r.get("attributes") or {}))
    return out


def _builtin_help_counts(events: list[tuple]) -> tuple[int, int]:
    """Count builtin help() calls and the subset targeting ava.* objects."""
    calls = 0
    on_ava = 0
    for event, payload in events:
        if event != "code":
            continue
        body = str(payload.get("body", ""))
        calls += len(_BUILTIN_HELP_CALL_RE.findall(body))
        on_ava += len(_BUILTIN_HELP_ON_AVA_RE.findall(body))
    return calls, on_ava


def _subprocess_call_count(events: list[tuple]) -> int:
    """Count raw subprocess calls across an agent's executed code blocks."""
    return sum(
        len(_SUBPROCESS_CALL_RE.findall(str(payload.get("body", ""))))
        for event, payload in events
        if event == "code"
    )


# ─────── Loki paging constants ─────────────────────────────────────────────
# Task #1197 (2026-08-12): PG `events` is a frozen archive; live events ship
# as OTLP logs and are read back through the gateway's Loki-backed
# /api/events endpoint. collect.py was a missed migration consumer
# (2026-08-14: the daily scan produced empty datasets for 36 hours).
#
# Paging discipline: every request is offset=0, limit=_PAGE; slices whose
# page overflows (has_more) are bisected in time. No count aggregation is
# requested — /api/events meta.total is opt-in (with_total=1, 2026-08-18
# change) and the count path is exactly the Loki load the gateway was
# shedding. Loki's default max_entries_limit_per_query is 5000 — offset
# paging silently drops rows once limit+offset+1 exceeds it (2026-08-14,
# agent 3012) and is timeout-prone, so offsets are never used.
_PAGE = 1000  # /api/events limit cap — each slice fits exactly one page
_HTTP_TIMEOUT_S = 120.0
_RETRIES = 2
_RETRY_BACKOFF_S = 5.0
_EVENTS_PATH = "/api/events"
_MIN_SLICE = timedelta(seconds=1)


def _inbounds_by_agent(
    cur: psycopg.Cursor, agent_ids: list[int], window: str
) -> dict[int, list[dict[str, Any]]]:
    """Fetch window-scoped inbound messages (user + agent sources) for the
    given agent_ids, plus each agent's first-ever chat message (the task
    prompt, which may predate the window for long-running agents).

    2026-08-13 fix: the previous no-window query loaded every inbound
    message an agent ever received, so `corrections` / `peer_feedback`
    accumulated lifetime history into each day's record — the daily scan
    flagged the whole fleet as fumbled every day (1336 "peer feedback"
    messages across 70 runs on a day when agents were fine).

    Returns dict[agent_id, list[{"source", "content", "is_broadcast"}]].
    A broadcast is one source/content pair delivered to multiple agents in the
    collection window. It remains visible in the record but does not count as
    a correction or re-prompt for each recipient.
    """
    # Task prompts: the first-ever chat message per agent, regardless of age.
    cur.execute(
        "SELECT DISTINCT ON (agent_id) id, agent_id, source, content "
        "FROM inbound_messages "
        "WHERE agent_id = ANY(%s) AND kind = 'chat' "
        "AND (source = ANY(%s) OR source LIKE 'agent:%%' OR source = 'system') "
        "ORDER BY agent_id, created_at",
        [agent_ids, TASK_SOURCES],
    )
    first_ids: dict[int, int] = {}
    first_rows: dict[int, dict[str, Any]] = {}
    for msg_id, agent_id, source, content in cur.fetchall():
        first_ids[agent_id] = msg_id
        first_rows[agent_id] = {"source": source, "content": content, "is_broadcast": False}
    # Everything the agent actually received during this collection window.
    cur.execute(
        "WITH window_inbounds AS ("
        "SELECT id, agent_id, source, content, created_at "
        "FROM inbound_messages "
        "WHERE agent_id = ANY(%s) AND kind = 'chat' "
        "AND (source = ANY(%s) OR source LIKE 'agent:%%' OR source = 'system') "
        "AND created_at > now() - %s::interval"
        "), broadcasts AS ("
        "SELECT source, content FROM window_inbounds "
        "GROUP BY source, content HAVING count(DISTINCT agent_id) > 1"
        ") "
        "SELECT inbound.id, inbound.agent_id, inbound.source, inbound.content, "
        "broadcasts.source IS NOT NULL AS is_broadcast "
        "FROM window_inbounds inbound "
        "LEFT JOIN broadcasts ON broadcasts.source = inbound.source "
        "AND broadcasts.content = inbound.content "
        "ORDER BY inbound.agent_id, inbound.created_at",
        [agent_ids, TASK_SOURCES, window],
    )
    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for msg_id, agent_id, source, content, is_broadcast in cur.fetchall():
        if msg_id == first_ids.get(agent_id):
            continue  # the task prompt — already first via prepend below
        out[agent_id].append({"source": source, "content": content, "is_broadcast": is_broadcast})
    # Prepend the task prompt so build_record still finds it at
    # user_msgs[0] / spawner_msgs[0].
    for agent_id, row in first_rows.items():
        out[agent_id].insert(0, row)
    return out


def _meta_by_agent(cur: psycopg.Cursor, ids: list[int]) -> dict[int, tuple]:
    cur.execute(
        "SELECT id, spawner, status, last_message_text FROM agents_meta WHERE id = ANY(%s)",
        [ids],
    )
    return {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}


def _test_label_ids(cur: psycopg.Cursor, ids: list[int]) -> set[int]:
    """Benchmark/probe agents and their direct workers, excluded from health data.

    A TEST- orchestrator's workers inherit its benchmark-only purpose even
    though their own labels describe the task they execute.
    """
    cur.execute(
        "SELECT child.id, child.label, parent.label "
        "FROM agents child "
        "LEFT JOIN agents_meta child_meta ON child_meta.id = child.id "
        "LEFT JOIN agents parent ON child_meta.spawner = 'agent:' || parent.id::text "
        "WHERE child.id = ANY(%s)",
        [ids],
    )
    return {
        row[0]
        for row in cur.fetchall()
        if (row[1] or "").startswith("TEST-") or (row[2] or "").startswith("TEST-")
    }


# ─────────────── driver ───────────────


def collect_with_counts(
    days: int,
    week: str,
    *,
    from_: datetime | None = None,
    to: datetime | None = None,
    include_test: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """collect() plus the pre-record counts the daily sentinel needs.

    Returns (records, counts) where counts is
    {"seen": distinct window agents with events,
     "excluded_test": dropped by the TEST- filter,
     "skipped_meta": dropped for a missing lifecycle row}.
    The daily scan's empty-dataset sentinel (2026-08-14: 0 runs = data
    source outage) keys on pre-filter activity, so a TEST-only window — a
    healthy source whose every run the filter removed by design — stays
    distinguishable from a source that produced nothing (QA review of
    PR #698, 2026-08-29).
    """
    window_to = to or datetime.now(UTC)
    window_from = from_ or (window_to - timedelta(days=days))
    if window_from >= window_to:
        raise ValueError(f"empty window: [{window_from}, {window_to})")
    telemetry = _fetch_events_window("telemetry", window_from, window_to)
    audit = _fetch_events_window("audit", window_from, window_to)
    events = _group_by_agent(telemetry)
    logs = _group_by_agent(audit)
    ids = sorted(events)
    window = f"{days} days"
    with connect() as conn, conn.cursor() as cur:
        inbounds = _inbounds_by_agent(cur, ids, window)
        meta = _meta_by_agent(cur, ids)
        test_ids = set() if include_test else _test_label_ids(cur, ids)
    records = []
    excluded_test = 0
    skipped_meta = 0
    for agent_id in ids:
        if agent_id not in meta:
            skipped_meta += 1
            continue  # active in events but no lifecycle row — skip, not a real run
        if agent_id in test_ids:
            excluded_test += 1
            continue  # benchmark/probe spawn — never into the health dataset
        agent_events = events.get(agent_id, [])
        record = build_record(
            agent_id,
            week,
            agent_events,
            logs.get(agent_id, []),
            inbounds.get(agent_id, []),
            meta[agent_id],
        )
        record["builtin_help_calls"], record["builtin_help_on_ava"] = _builtin_help_counts(
            agent_events
        )
        record["subprocess_calls"] = _subprocess_call_count(agent_events)
        records.append(record)
    counts = {"seen": len(ids), "excluded_test": excluded_test, "skipped_meta": skipped_meta}
    return records, counts


def collect(
    days: int,
    week: str,
    *,
    from_: datetime | None = None,
    to: datetime | None = None,
    include_test: bool = False,
) -> list[dict[str, Any]]:
    """One record per agent active in the window [from_, to) (default:
    now - days -> now, UTC).

    Events come from Loki via the gateway /api/events endpoint (PG `events`
    is a frozen archive since 2026-08-12, Task #1197); inbounds and lifecycle
    metadata still come from PG. Category 'log' is not fetched — it carries
    only exec stdout payloads, which build_record never reads, and every
    agent that logs stdout also emits telemetry events, so agent discovery
    loses nothing.

    Agents whose role label carries the TEST- prefix (benchmark / probe
    spawns) are excluded by default so they never reach the health dataset
    or its labels; pass `include_test=True` to collect them (a measurement
    run that reads the same fields).

    Callers that must tell a TEST-only window from a broken data source
    use collect_with_counts(), which returns the same records plus the
    pre-filter counts.
    """
    return collect_with_counts(days, week, from_=from_, to=to, include_test=include_test)[0]


def collect_one(
    agent_id: int,
    week: str | None = None,
    leak_paths: LeakPaths | None = None,
) -> dict[str, Any]:
    """Build a trace record for a single agent by id — its rows from the last
    7 days (Loki retention; eval agents live minutes, so this captures their
    full lifetime). Used to score a freshly-spawned evaluation agent right
    after it finishes. Unlike the weekly collect, inbounds are not filtered
    to user sources — an eval agent's task prompt arrives from its spawner
    (`agent:N`), so every chat inbound counts."""
    week = week or _default_week()
    now = datetime.now(UTC)
    window_from = now - timedelta(days=7)
    telemetry = _fetch_events_window("telemetry", window_from, now, agent_id=agent_id)
    audit = _fetch_events_window("audit", window_from, now, agent_id=agent_id)
    events = [
        (r["event_name"], r.get("attributes") or {})
        for r in telemetry
        if r.get("agent_id") == agent_id
    ]
    log_events = [
        (r["event_name"], r.get("attributes") or {}) for r in audit if r.get("agent_id") == agent_id
    ]
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT source, content FROM inbound_messages WHERE agent_id = %s AND kind = 'chat' "
            "ORDER BY created_at",
            [agent_id],
        )
        inbounds: list[dict[str, Any]] = [
            {"source": source, "content": content, "is_broadcast": False}
            for source, content in cur.fetchall()
        ]
        cur.execute(
            "SELECT spawner, status, last_message_text FROM agents_meta WHERE id = %s", [agent_id]
        )
        meta = cur.fetchone()
    if meta is None:
        raise ValueError(f"no agents_meta row for agent {agent_id}")
    record = build_record(agent_id, week, events, log_events, inbounds, meta, leak_paths=leak_paths)
    record["builtin_help_calls"], record["builtin_help_on_ava"] = _builtin_help_counts(events)
    record["subprocess_calls"] = _subprocess_call_count(events)
    return record


def dataset_path(week: str) -> Path:
    out_dir = ava_home() / "self_evolution" / "dataset"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{week}.jsonl"


def write_dataset(records: list[dict[str, Any]], week: str) -> Path:
    """Write dataset to a dated JSONL file. Pass an explicit `--week` to
    regenerate a previous file in place; the default uses today's date."""
    path = dataset_path(week)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    return path


def _default_week() -> str:
    # Use today's date as the week label so each manual run (or schedule run
    # on Monday) gets a distinct file.  The schedule fires on Monday at 9 AM
    # so its runs are dated naturally; mid-week manual runs won't collide.
    return datetime.now(UTC).date().isoformat()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Harvest the past week's agent runs into a trace dataset (JSONL).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--days", type=int, default=7, help="window size in days (default 7)")
    p.add_argument(
        "--week", default=None, help="week label / output filename stem (default: this Monday)"
    )
    p.add_argument(
        "--from",
        dest="from_",
        default=None,
        help="window start, ISO-8601 with timezone (default: now - days) — use to backfill a missed day",
    )
    p.add_argument(
        "--to",
        default=None,
        help="window end, ISO-8601 with timezone (default: now)",
    )
    p.add_argument(
        "--include-test",
        action="store_true",
        help="include TEST- prefixed benchmark/probe spawns (measurement only; default excludes)",
    )
    return p.parse_args()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"window bounds must carry a timezone offset: {value!r}")
    return parsed.astimezone(UTC)


def main() -> None:
    args = parse_args()
    week = args.week or _default_week()
    from_ = _parse_iso(args.from_) if args.from_ else None
    to = _parse_iso(args.to) if args.to else None
    records = collect(args.days, week, from_=from_, to=to, include_test=args.include_test)
    path = write_dataset(records, week)
    counts = Counter(r["label"] for r in records)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(
        f"[{stamp}] collected {len(records)} runs "
        f"(ok {counts['ok']} / fumbled {counts['fumbled']} / failed {counts['failed']}) "
        f"-> {path}"
    )


if __name__ == "__main__":
    main()
