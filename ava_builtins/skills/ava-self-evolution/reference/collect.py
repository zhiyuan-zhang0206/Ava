#!/usr/bin/env python3
"""Harvest the past week's real agent runs into a trace dataset file.

One JSON record per agent that was active in the window, written as JSONL to
`$AVA_HOME/self_evolution/dataset/<week>.jsonl`. Each record is assembled from
existing tables only (no new schema): the unified `events` stream —
`category=telemetry/log` carries token / turn / exec signals, `category=audit`
carries compaction, delivery breach, and the future
`skill_invoked` hard signal), `inbound_messages` (the user's task prompt and
any follow-up corrections), `agents_meta` (spawner, terminal status, final
message), and the stored checkpoint (the full message transcript).

The dataset is the durable asset this whole skill iterates on: real tasks,
real traces, real outcomes. It lives under `$AVA_HOME` (per-deployment,
private) so the skill package itself stays publishable.

Run it standalone (the skill directory name has a hyphen, so these files are
scripts, not an importable package):

    .venv/bin/python skills/ava-self-evolution/reference/collect.py --days 7

`collect.py` imports its sibling `label.py`; run it as a script so the
reference directory is on `sys.path`.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
from label import label  # sibling script, resolved via sys.path[0]
from pydantic import ValidationError

from shared.audit_events import SkillInvokedPayload
from shared.checkpoint import CheckpointReadError, load_checkpoint_messages
from shared.db import connect
from shared.paths import ava_home

# Task-origin inbound sources — capture every real task prompt, whether from
# the user ("user") or a spawner / peer agent ("agent:<id>"). System and
# watcher messages are deliberately excluded (nudges, heartbeats, wake-ups).
TASK_SOURCES = ["user"]  # plus "agent:%" matched via LIKE in the query

# ─────────────── correction / feedback detection ───────────────

# Chinese correction keywords — user is redirecting or correcting the agent.
CN_CORRECTION_KEYWORDS = [
    "不对",
    "错了",
    "重新",
    "不要",
    "改成",
    "应该是",
    "不是这样",
    "你搞错了",
    "搞错了",
    "纠正",
    "修正一下",
    "不是这个",
    "别这样",
    "不是让你",
    "你没理解",
    "理解错了",
    "错误",
    "有误",
    "误导",
]

# English correction keywords — user is redirecting or correcting the agent.
EN_CORRECTION_KEYWORDS = [
    "wrong",
    "incorrect",
    "don't",
    "do not",
    "should be",
    "instead",
    "actually",
    "no,",
    "fix this",
    "correct this",
    "not right",
    "mistake",
    "you misunderstood",
    "that's not",
    "redo",
    "re-do",
    "try again",
]

# Agent-to-agent feedback keywords — one agent correcting or reminding another.
PEER_FEEDBACK_KEYWORDS = [
    "你那个",
    "你没",
    "不要用",
    "别用",
    "改成",
    "修一下",
    "还没修",
    "没修好",
    "不对",
    "错了",
    "应该用",
    "不能这样",
    "注意",
    "别忘了",
    "提醒",
    "纠正",
]


def _detect_correction(content: str) -> bool:
    """Return True if `content` reads as a user correction rather than a
    neutral instruction or follow-up question.

    Checks Chinese keywords first (most of our user traffic), then English.
    The check is substring-based, case-insensitive for English.
    """
    if not content:
        return False
    if any(kw in content for kw in CN_CORRECTION_KEYWORDS):
        return True
    content_lower = content.lower()
    return any(kw in content_lower for kw in EN_CORRECTION_KEYWORDS)


def _detect_peer_feedback(content: str) -> bool:
    """Return True if an agent-to-agent message reads as corrective feedback
    rather than a neutral status update or coordination message.

    Uses a focused keyword set tuned for our fleet's communication patterns.
    Also falls back to the general correction detector for overlap.
    """
    if not content:
        return False
    if any(kw in content for kw in PEER_FEEDBACK_KEYWORDS):
        return True
    # Also check general correction patterns — agent feedback often uses
    # the same language as user corrections.
    return _detect_correction(content)


# ─────────────── tool-call counting (AST over executed code) ───────────────


def _dotted(node: ast.AST) -> str | None:
    """Resolve an attribute chain like `ava.files.edit` to its dotted string,
    or None if the call target is not a plain attribute-on-name chain."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def count_tool_calls(codes: list[str]) -> dict[str, int]:
    """Count `ava.*` SDK calls across a run's executed code blocks. Code that
    does not parse is skipped (the model occasionally emits a broken block)."""
    counts: Counter[str] = Counter()
    for code in codes:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = _dotted(node.func)
                if name and name.split(".", 1)[0] == "ava":
                    counts[name] += 1
    return dict(counts)


# ─────────────── exec-outcome helpers (mirror shared.metrics) ───────────────


def _is_exec_ok(event: str) -> bool:
    return event == "exec"


def _is_exec_fail(event: str) -> bool:
    return event != "exec" and event.startswith(("exec_", "exec("))


# ─────────────── message serialization ───────────────


def _msg_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("type") or "")
            else:
                parts.append(str(block))
        return " ".join(p for p in parts if p)
    return str(content)


def _transcript(agent_id: int) -> list[dict[str, str]]:
    """Full stored message list for one agent, or [] if unreadable."""
    try:
        msgs = load_checkpoint_messages(agent_id)
    except CheckpointReadError:
        return []
    return [{"type": m.type, "content": _msg_text(m.content)} for m in msgs]


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
    return os.environ.get("AVA_GATEWAY_URL", "http://localhost:8000")


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
        # the exact slice count drives the bisection; total is opt-in
        # server-side (skipped by default to spare the count aggregation)
        "with_total": 1,
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
    every request is offset=0 with limit=_PAGE, and any slice whose exact
    `meta.total` exceeds _PAGE is bisected in time until each half fits one
    page. Rows are deduped by surrogate row id across slice boundaries (the
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
            total = int(meta.get("total", 0))
            if total == 0:
                return
            if total <= _PAGE or end - start <= _MIN_SLICE:
                if total > _PAGE:
                    print(
                        f"warning: {category} slice [{start}, {end}] has {total} rows "
                        f"over the page budget ({_PAGE}) — tail may be truncated"
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


# ─────── Loki paging constants ─────────────────────────────────────────────
# Task #1197 (2026-08-12): PG `events` is a frozen archive; live events ship
# as OTLP logs and are read back through the gateway's Loki-backed
# /api/events endpoint. collect.py was a missed migration consumer
# (2026-08-14: the daily scan produced empty datasets for 36 hours).
#
# Paging discipline: every request is offset=0, limit=_PAGE; slices whose
# exact total exceeds _PAGE are bisected in time. Loki's default
# max_entries_limit_per_query is 5000 — /api/events offset paging silently
# drops rows once limit+offset+1 exceeds it (2026-08-14, agent 3012) and is
# timeout-prone, so offsets are never used.
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

    Returns dict[agent_id, list[{"source": str, "content": str}]].
    Source is "user" for user messages, "agent:<id>" for peer messages.
    """
    base_sql = (
        "SELECT id, agent_id, source, content FROM inbound_messages "
        "WHERE agent_id = ANY(%s) AND kind = 'chat' "
        "AND (source = ANY(%s) OR source LIKE 'agent:%%' OR source = 'system') "
    )
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
        first_rows[agent_id] = {"source": source, "content": content}
    # Everything the agent actually received during this collection window.
    cur.execute(
        base_sql + "AND created_at > now() - %s::interval ORDER BY agent_id, created_at",
        [agent_ids, TASK_SOURCES, window],
    )
    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for msg_id, agent_id, source, content in cur.fetchall():
        if msg_id == first_ids.get(agent_id):
            continue  # the task prompt — already first via prepend below
        out[agent_id].append({"source": source, "content": content})
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


# ─────────────── skill attribution ───────────────


def _skills_touched(log_events: list[tuple]) -> list[str]:
    """Skills actually invoked by this agent during the run.

    `skill_invoked` event_log rows carry an `invocation_depth` field:
    - `"loaded"` — the agent explicitly accessed the skill (high signal)
    - `"prompt_injected"` — the skill was listed in the system prompt
      but the agent never touched it (noise — almost every agent
      "touches" gmail, ui, worktree this way)

    Only `loaded` skills count. The regex soft-signal fallback was removed
    with the event-system rework (2026-08): prompt_injected rows no longer
    exist, so "no skill_invoked rows" no longer means "framework not landed"
    — it means the agent did not load any skill. (Soft-signal scanning also
    mis-attributed prompt_injected names from the transcript, the very noise
    the rework removes.)
    """
    loaded: set[str] = set()
    for event_type, payload in log_events:
        if event_type != "skill_invoked":
            continue
        # Best-effort scoring: a malformed row is skipped, never crashes the
        # weekly collect (same graceful-drift tolerance the old dict .get had).
        with contextlib.suppress(ValidationError):
            entry = SkillInvokedPayload.model_validate(payload)
            if entry.invocation_depth == "loaded":
                loaded.add(entry.skill)
    return sorted(loaded)


def _plugins_activated(events: list[tuple]) -> dict[str, int]:
    """Plugin injection surfaces that fired during the run -> how many times.

    The plugin-side counterpart of `_skills_touched`. Hooks and wraps used to
    run silently, so a plugin that rewrote state or short-circuited an SDK call
    in a bad run left no trace in this dataset and plugin-caused regressions
    could not be mined. `plugin_activation` telemetry (issue #40) closes that:
    one event per firing, carrying the same `(plugin, surface, identifier)`
    triple `ava plugins inspect` shows as a registered contribution.

    Keyed `"<plugin>/<surface>/<identifier>"` so a regression can be attributed
    to the specific contribution, not merely to the plugin. A malformed row is
    skipped rather than crashing the weekly collect — same graceful-drift
    tolerance `_skills_touched` applies.
    """
    fired: Counter[str] = Counter()
    for event_type, payload in events:
        if event_type != "plugin_activation":
            continue
        p = payload or {}
        plugin, surface, identifier = p.get("plugin"), p.get("surface"), p.get("identifier")
        if not (plugin and surface and identifier):
            continue
        fired[f"{plugin}/{surface}/{identifier}"] += 1
    return dict(sorted(fired.items()))


# ─────────────── per-agent assembly ───────────────


def _mode(values: list[str]) -> str:
    return Counter(v for v in values if v).most_common(1)[0][0] if any(values) else ""


def build_record(
    agent_id: int,
    week: str,
    events: list[tuple],
    log_events: list[tuple],
    inbounds: list[dict[str, Any]],
    meta: tuple,
) -> dict[str, Any]:
    spawner, status, last_message_text = meta

    # ── split inbounds by source ──
    user_msgs = [m["content"] for m in inbounds if m["source"] == "user"]
    agent_msgs = [m for m in inbounds if m["source"].startswith("agent:")]

    # ── classify user inbounds ──
    # First user message is always the task prompt. Subsequent messages are
    # split into corrections (redirection / criticism) and neutral follow-ups.
    task_prompt = user_msgs[0] if user_msgs else ""
    # Agent-spawned agents receive their task from the spawner, not the user.
    # The first message from the spawner IS the task prompt.
    if not task_prompt.strip() and spawner.startswith("agent:"):
        spawner_msgs = [m for m in agent_msgs if m["source"] == spawner]
        if spawner_msgs:
            task_prompt = spawner_msgs[0]["content"]
    # Cron / system agents receive their task via source='system'.
    # The first system chat message is the task prompt.
    if not task_prompt.strip():
        system_msgs = [m["content"] for m in inbounds if m["source"] == "system"]
        if system_msgs:
            task_prompt = system_msgs[0]
    corrections: list[str] = []
    followup_prompts: list[str] = []
    for msg in user_msgs[1:]:
        if _detect_correction(msg):
            corrections.append(msg)
        else:
            followup_prompts.append(msg)

    # ── classify agent-to-agent inbounds ──
    # Separate corrective peer feedback from neutral coordination messages.
    # The first message from the spawner is the task assignment, not feedback —
    # skip it.  Subsequent messages from the same spawner, and messages from
    # other agents, are candidates.
    spawner_id: int | None = None
    if spawner.startswith("agent:"):
        with contextlib.suppress(IndexError, ValueError):
            spawner_id = int(spawner.split(":", 1)[1])
    peer_feedback: list[dict[str, Any]] = []
    for m in agent_msgs:
        try:
            from_agent = int(m["source"].split(":", 1)[1])
        except (IndexError, ValueError):
            from_agent = 0
        # Skip all messages from the spawner — they are task coordination,
        # not corrective feedback.  Only messages from other agents count.
        if from_agent == spawner_id:
            continue
        if _detect_peer_feedback(m["content"]):
            peer_feedback.append(
                {
                    "from_agent": from_agent,
                    "content": m["content"],
                }
            )

    turns = sum(1 for e, _ in events if e == "turn_end")
    models = [str(p.get("model", "")) for e, p in events if e == "llm_usage"]
    tokens_in = sum(int(p.get("in_total", 0)) for e, p in events if e == "llm_usage")
    tokens_out = sum(int(p.get("out_total", 0)) for e, p in events if e == "llm_usage")
    code_bodies = [str(p.get("body", "")) for e, p in events if e == "code"]
    exec_ok = sum(1 for e, _ in events if _is_exec_ok(e))
    exec_failed = sum(1 for e, _ in events if _is_exec_fail(e))
    exec_outcomes = [e for e, _ in events if _is_exec_ok(e) or _is_exec_fail(e)]
    last_exec_failed = bool(exec_outcomes) and _is_exec_fail(exec_outcomes[-1])
    durations = [float(p.get("duration_seconds", 0)) for e, p in events if e == "turn_end"]
    exec_duration_s = sum(
        float((p or {}).get("duration_seconds", 0))
        for e, p in events
        if e == "node_exit" and (p or {}).get("node") == "exec"
    )

    compactions = sum(1 for et, _ in log_events if et == "compact")
    breached = any(et == "report_breached" for et, _ in log_events)

    transcript = _transcript(agent_id)

    rec: dict[str, Any] = {
        "agent_id": agent_id,
        "week": week,
        "spawner": spawner,
        "model": _mode(models),
        "task_prompt": task_prompt,
        "followup_prompts": followup_prompts,
        "corrections": corrections,
        "peer_feedback": peer_feedback,
        "transcript": transcript,
        "final_output": last_message_text or "",
        "turns": turns,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tools_called": count_tool_calls(code_bodies),
        "skills_touched": _skills_touched(log_events),
        "plugins_activated": _plugins_activated(events),
        "exec_ok": exec_ok,
        "exec_failed": exec_failed,
        "last_exec_failed": last_exec_failed,
        "compactions": compactions,
        "breached": breached,
        "terminated": status == "terminated",
        "duration_s": round(sum(durations), 1),
    }
    rec["label"] = label(rec)
    return rec


# ─────────────── driver ───────────────


def collect(
    days: int,
    week: str,
    *,
    from_: datetime | None = None,
    to: datetime | None = None,
) -> list[dict[str, Any]]:
    """One record per agent active in the window [from_, to) (default:
    now - days -> now, UTC).

    Events come from Loki via the gateway /api/events endpoint (PG `events`
    is a frozen archive since 2026-08-12, Task #1197); inbounds and lifecycle
    metadata still come from PG. Category 'log' is not fetched — it carries
    only exec stdout payloads, which build_record never reads, and every
    agent that logs stdout also emits telemetry events, so agent discovery
    loses nothing.
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
    records = []
    for agent_id in ids:
        if agent_id not in meta:
            continue  # active in events but no lifecycle row — skip, not a real run
        records.append(
            build_record(
                agent_id,
                week,
                events.get(agent_id, []),
                logs.get(agent_id, []),
                inbounds.get(agent_id, []),
                meta[agent_id],
            )
        )
    return records


def collect_one(agent_id: int, week: str | None = None) -> dict[str, Any]:
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
            {"source": source, "content": content} for source, content in cur.fetchall()
        ]
        cur.execute(
            "SELECT spawner, status, last_message_text FROM agents_meta WHERE id = %s", [agent_id]
        )
        meta = cur.fetchone()
    if meta is None:
        raise ValueError(f"no agents_meta row for agent {agent_id}")
    return build_record(agent_id, week, events, log_events, inbounds, meta)


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
    records = collect(args.days, week, from_=from_, to=to)
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
