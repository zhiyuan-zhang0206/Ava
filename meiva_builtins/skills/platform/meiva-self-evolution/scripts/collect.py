#!/usr/bin/env python3
"""Harvest the past week's real agent runs into a trace dataset file.

One JSON record per agent that was active in the window, written as JSONL to
`$MEIVA_HOME/self_evolution/dataset/<week>.jsonl`. Each record is assembled from
existing tables only (no new schema): the unified `events` stream —
`category=telemetry/log` carries token / turn / exec signals, `category=audit`
carries compaction and the future
`skill_invoked` hard signal), `inbound_messages` (the user's task prompt and
any follow-up corrections), `agents_meta` (spawner, terminal status, final
message), and the stored checkpoint (the full transcript reconstructed across
compaction boundaries retained by Task #1125; only duplicate system prompts
are removed, fixing the 2026-08-10 truncation without hiding lived messages).

The dataset is the durable asset this whole skill iterates on: real tasks,
real traces, real outcomes. It lives under `$MEIVA_HOME` (per-deployment,
private) so the skill package itself stays publishable.

Usage:

    .venv/bin/python skills/meiva-self-evolution/scripts/collect.py --days 7
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg

from base.db import Database

# `record` is a sibling in this skill's own scripts/ dir, not an importable
# package (PYTHONSAFEPATH=1 keeps a script's own directory off sys.path) —
# restore it for the sibling import. Structure Rule 6 recognizes this exact
# __file__-derived, within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from record import HUMAN_SOURCES, LeakPaths, build_record

# re-exported below (tests reach these through collect.* to patch the
# collection pipeline's transcript/plugin-count internals): `as` self-aliases
# so pyright recognizes them as intentional, not unused, imports.
from record import _plugins_activated as _plugins_activated
from record import _transcript as _transcript

from base.cluster.machine import gateway_auth_headers
from base.cluster.machine.ambient import gateway_access_from_settings
from base.config import settings
from base.host.net.resilience import ExponentialBackoff, Policy, http_classifier, retry
from base.paths import meiva_home

_BUILTIN_HELP_CALL_RE = re.compile(r"(?<![a-zA-Z0-9_.])help\s*\(")
_BUILTIN_HELP_ON_MEIVA_RE = re.compile(r"(?<![a-zA-Z0-9_.])help\s*\(\s*meiva\.")
_SUBPROCESS_CALL_RE = re.compile(r"subprocess\.(run|Popen|check_output|check_call|call)\b")


# ─────── Loki-backed event fetches (paged via the gateway /api/events) ───────


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
    """One /api/events page (offset always 0). Returns (items, meta).

    Runs on `_EVENTS_RETRY_POLICY`: a transient failure (502/503/504 or a
    transport error) is retried inside the wait budget; a spent budget
    raises the diagnostics-carrying error, any other status fails fast.
    """
    params: dict[str, Any] = {
        "category": category,
        "from": from_.isoformat(),
        "to": to.isoformat(),
        "limit": _PAGE,
        "offset": 0,
    }
    if agent_id is not None:
        params["agent_id"] = agent_id

    def fetch() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        access = gateway_access_from_settings()  # ambient: O
        resp = client.get(
            _gateway_url() + _EVENTS_PATH,
            params=params,
            headers=gateway_auth_headers(access),
            timeout=_HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        payload = resp.json()
        return payload.get("items", []), payload.get("meta", {})

    return retry(_EVENTS_RETRY_POLICY)(fetch)


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
    window must not silently become a partial dataset; a spent retry
    budget raises with the responder's status, headers, and body snippet.
    """
    out: list[dict[str, Any]] = []
    seen: set[int] = set()

    # trust_env=False (2026-09-12): cluster-internal reads must not be
    # routed through an env-configured proxy. httpx otherwise honors
    # HTTP(S)_PROXY / NO_PROXY, and a no_proxy form it does not match hands
    # these requests to a local forward proxy that cannot reach the gateway
    # — which answered 502 while the gateway was healthy. Same pattern as
    # ops/cluster/rpc.py and cli/commands/observability/trace.py, for the same reason.
    with httpx.Client(timeout=_HTTP_TIMEOUT_S, trust_env=False) as client:

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
    """Count builtin help() calls and the subset targeting meiva.* objects."""
    calls = 0
    on_meiva = 0
    for event, payload in events:
        if event != "code":
            continue
        body = str(payload.get("body", ""))
        calls += len(_BUILTIN_HELP_CALL_RE.findall(body))
        on_meiva += len(_BUILTIN_HELP_ON_MEIVA_RE.findall(body))
    return calls, on_meiva


def _subprocess_call_count(events: list[tuple]) -> int:
    """Count raw subprocess calls across executed code blocks with a regex.

    This includes matching names in comments, docstrings, and string literals.
    It does not count ``asyncio.create_subprocess_*`` or ``subprocess.getoutput``.
    """
    return sum(
        len(_SUBPROCESS_CALL_RE.findall(str(payload.get("body", ""))))
        for event, payload in events
        if event == "code"
    )


# ─────── Loki paging constants ─────────────────────────────────────────────
# Events ship as OTLP logs and are read back through the gateway's Loki-backed
# /api/events endpoint.
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
_EVENTS_PATH = "/api/events"
_MIN_SLICE = timedelta(seconds=1)


# ─────── /api/events retry budget + failure diagnostics ─────────────────────
# Task #3126 (2026-09-12): a transient blip at the daily scan must not cost
# the day's dataset. Only 502/503/504 and transport errors are retryable: a
# forwarding hop that failed (2026-09-12: an env-configured local proxy
# answered 502 for a healthy gateway) or the gateway shedding (503/504).
# Narrower than the shared 429/5xx default on purpose — any other status is
# structural for this once-a-day read: fail fast so the scan reports the
# real error instead of spending the budget on it.
_EVENTS_CLASSIFY = http_classifier.with_(
    transient={502, 503, 504},
    permanent={429, 500},  # the shared default's other transient statuses
)

# Length cap for one diagnostic field (headers / body): a proxy's HTML page
# or a Loki error payload must not flood the scan's log line.
_DIAG_FIELD_CHARS = 300


def _bounded(text: str) -> str:
    """One-line, length-bounded excerpt of a diagnostic field."""
    text = " ".join(text.split())
    if len(text) <= _DIAG_FIELD_CHARS:
        return text
    return f"{text[:_DIAG_FIELD_CHARS]}... (+{len(text) - _DIAG_FIELD_CHARS} chars)"


def _exhausted_fetch_error(exc: Exception) -> RuntimeError:
    """The error raised once the retry budget is spent.

    Carries the evidence that identifies the responder without re-running
    the scan: for an HTTP failure the status line plus bounded header/body
    excerpts — a local forward proxy's 502 and the gateway's own 503 are
    otherwise indistinguishable — and for a transport failure its own name
    and message.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        detail = (
            f"HTTP {response.status_code} {response.reason_phrase}; "
            f"headers: {_bounded(str(dict(response.headers)))}; "
            f"body: {_bounded(response.text)}"
        )
    else:
        detail = f"{type(exc).__name__}: {exc}"
    attempts = _EVENTS_RETRY_POLICY.max_attempts
    return RuntimeError(f"/api/events read failed after {attempts} attempts: {detail}")


def _convert_retry_exhausted(exc: Exception) -> None:
    """on_final_failure hook: convert a spent budget, pass a fail-fast through.

    The retry executor calls this on every final failure; a retryable class
    reaches it only with the budget spent (otherwise it would have been
    retried), while a permanent class failed fast on the first attempt and
    re-raises untouched.
    """
    if _EVENTS_CLASSIFY(exc):
        raise _exhausted_fetch_error(exc) from exc


# Five attempts, sleeps 5/10/20/40 s — a ~75 s total wait budget (task
# #3126: 60-90 s). Jitter is off (a single scheduled runner, nobody to
# de-phase) and Retry-After is not honored: the budget is exactly this
# schedule, so the worst case stays known.
_EVENTS_RETRY_POLICY = Policy(
    max_attempts=5,
    backoff=ExponentialBackoff(base=5.0, factor=2.0, cap=40.0),
    classify=_EVENTS_CLASSIFY,
    jitter="none",
    respect_retry_after=False,
    on_final_failure=_convert_retry_exhausted,
)


# meiva:ref meiva_builtins/skills/platform/meiva-self-evolution/docs/collection.meiva.okf.md#inbound-scoping
def _inbounds_by_agent(
    cur: psycopg.Cursor, agent_ids: list[int], window_from: datetime, window_to: datetime
) -> dict[int, list[dict[str, Any]]]:
    cur.execute(
        "SELECT DISTINCT ON (inbound.agent_id, inbound.source) "
        "inbound.id, inbound.agent_id, inbound.source, inbound.content, "
        "inbound.claimed_at IS NOT NULL AS claimed, inbound.created_at "
        "FROM inbound_messages inbound "
        "LEFT JOIN agents_meta meta ON meta.id = inbound.agent_id "
        "WHERE inbound.agent_id = ANY(%s) AND inbound.kind = 'chat' "
        "AND (inbound.source = meta.spawner OR inbound.source = ANY(%s) "
        "OR inbound.source LIKE 'schedule:%%') "
        "ORDER BY inbound.agent_id, inbound.source, inbound.created_at",
        [agent_ids, sorted(HUMAN_SOURCES)],
    )
    task_rows = cur.fetchall()
    task_ids = {row[0] for row in task_rows}
    cur.execute(
        "WITH window_inbounds AS ("
        "SELECT id, agent_id, source, content, claimed_at IS NOT NULL AS claimed, created_at "
        "FROM inbound_messages "
        "WHERE agent_id = ANY(%s) AND kind = 'chat' "
        "AND (source = ANY(%s) OR source LIKE 'agent:%%') "
        "AND created_at >= %s AND created_at < %s"
        "), broadcasts AS ("
        "SELECT source, content FROM window_inbounds "
        "GROUP BY source, content HAVING count(DISTINCT agent_id) > 1"
        ") "
        "SELECT inbound.id, inbound.agent_id, inbound.source, inbound.content, "
        "inbound.claimed, inbound.created_at, broadcasts.source IS NOT NULL AS is_broadcast "
        "FROM window_inbounds inbound "
        "LEFT JOIN broadcasts ON broadcasts.source = inbound.source "
        "AND broadcasts.content = inbound.content "
        "ORDER BY inbound.agent_id, inbound.created_at",
        [agent_ids, sorted(HUMAN_SOURCES), window_from, window_to],
    )
    rows: dict[int, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
    for _msg_id, agent_id, source, content, claimed, created_at in task_rows:
        rows[agent_id].append(
            (
                created_at,
                {
                    "source": source,
                    "content": content,
                    "is_broadcast": False,
                    "claimed": claimed,
                },
            )
        )
    for msg_id, agent_id, source, content, claimed, created_at, is_broadcast in cur.fetchall():
        if msg_id in task_ids:
            continue
        rows[agent_id].append(
            (
                created_at,
                {
                    "source": source,
                    "content": content,
                    "is_broadcast": is_broadcast,
                    "claimed": claimed,
                },
            )
        )
    return {
        agent_id: [message for _, message in sorted(entries, key=lambda entry: entry[0])]
        for agent_id, entries in rows.items()
    }


def _task_receipts_by_agent(cur: psycopg.Cursor, ids: list[int]) -> dict[int, dict[str, int]]:
    cur.execute(
        "WITH status_receipts AS ("
        "SELECT actor_agent_id, task_id, request->>'status' AS declared, "
        "lag(request->>'status') OVER ("
        "PARTITION BY actor_agent_id, task_id ORDER BY accepted_at, operation_key"
        ") AS previous "
        "FROM task_update_receipts "
        "WHERE actor_agent_id = ANY(%s) AND request->>'status' IS NOT NULL"
        "), state_changes AS ("
        "SELECT actor_agent_id, count(*) AS changes FROM status_receipts "
        "WHERE declared IS DISTINCT FROM COALESCE(previous, 'in_progress') "
        "GROUP BY actor_agent_id"
        ") "
        "SELECT r.actor_agent_id, "
        "count(*) FILTER ("
        "WHERE r.request->>'note' IS NOT NULL AND r.request->>'status' IS NULL"
        "), "
        "count(*) FILTER (WHERE r.request->>'status' IS NOT NULL), "
        "count(*) FILTER (WHERE r.request->>'results' IS NOT NULL), "
        "COALESCE(max(c.changes), 0) "
        "FROM task_update_receipts r "
        "LEFT JOIN state_changes c ON c.actor_agent_id = r.actor_agent_id "
        "WHERE r.actor_agent_id = ANY(%s) "
        "GROUP BY r.actor_agent_id",
        [ids, ids],
    )
    return {
        row[0]: {"note": row[1], "status": row[2], "results": row[3], "state_changes": row[4]}
        for row in cur.fetchall()
    }


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
    database: Database,
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
    with database.connect() as conn, conn.cursor() as cur:
        inbounds = _inbounds_by_agent(cur, ids, window_from, window_to)
        meta = _meta_by_agent(cur, ids)
        receipts = _task_receipts_by_agent(cur, ids)
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
            database=database,
            task_receipts=receipts.get(agent_id),
        )
        record["builtin_help_calls"], record["builtin_help_on_meiva"] = _builtin_help_counts(
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
    database: Database,
    from_: datetime | None = None,
    to: datetime | None = None,
    include_test: bool = False,
) -> list[dict[str, Any]]:
    """One record per agent active in the window [from_, to) (default:
    now - days -> now, UTC).

    Events come from Loki via the gateway /api/events endpoint; inbounds and
    lifecycle metadata come from PG. Category 'log' is not fetched — it carries
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
    return collect_with_counts(
        days, week, database=database, from_=from_, to=to, include_test=include_test
    )[0]


def collect_one(
    agent_id: int,
    week: str | None = None,
    leak_paths: LeakPaths | None = None,
    *,
    database: Database,
) -> dict[str, Any]:
    # meiva:ref meiva_builtins/skills/platform/meiva-self-evolution/docs/collection.meiva.okf.md#inbound-scoping
    week = week or default_week()
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
    with database.connect() as conn, conn.cursor() as cur:
        inbounds = _inbounds_by_agent(cur, [agent_id], window_from, now).get(agent_id, [])
        cur.execute(
            "SELECT spawner, status, last_message_text FROM agents_meta WHERE id = %s", [agent_id]
        )
        meta = cur.fetchone()
        receipts = _task_receipts_by_agent(cur, [agent_id]).get(agent_id)
    if meta is None:
        raise ValueError(f"no agents_meta row for agent {agent_id}")
    record = build_record(
        agent_id,
        week,
        events,
        log_events,
        inbounds,
        meta,
        database=database,
        leak_paths=leak_paths,
        task_receipts=receipts,
    )
    record["builtin_help_calls"], record["builtin_help_on_meiva"] = _builtin_help_counts(events)
    record["subprocess_calls"] = _subprocess_call_count(events)
    return record


def dataset_path(week: str) -> Path:
    out_dir = meiva_home() / "self_evolution" / "dataset"
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


def default_week() -> str:
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
    from base.db.code_version_gate import ProcessDbGate
    from base.native_process.code_version import CodeVersion
    from base.native_process.loaded_commit import LoadedCommit

    version = CodeVersion(LoadedCommit.capture())
    gate = ProcessDbGate(version=version.get, process="unknown")
    args = parse_args()
    week = args.week or default_week()
    from_ = _parse_iso(args.from_) if args.from_ else None
    to = _parse_iso(args.to) if args.to else None
    database = Database.from_settings(gate=gate)
    records = collect(
        args.days, week, database=database, from_=from_, to=to, include_test=args.include_test
    )
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
