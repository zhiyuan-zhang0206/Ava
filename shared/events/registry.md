# Ava event name registry

> This document is **generated**: produced by `scripts/gen_event_registry.py` from
> the `EVENTS` registry in `shared/events/contract.py` (since 2026-08-08, R2-C).
> Hand edits are flagged as drift by the pre-commit `events-registry-fresh` hook.
> Inventory method and history: see §8.

> **Terminology (aligned to the OTel 2026 model on 2026-08-06, task #898)**: `kind` is
> the old name — in the unified event model every event is a named LogRecord, and the
> name field is OTel `event.name` (code and the DB column are unified as
> `event_name`). Each entry in this document = one `event_name` value; the SSE-side
> discriminator is called `role` (§5, live projection, not persisted).

**The registry is the single source of truth for event_name** (the `EVENTS` in
`shared/events/contract.py`). A new event = one row added to the registry
(`telemetry.emit` fails fast on unregistered names); the tables in this document are
generated from it and never hand-synced. event_names that violate the naming rules
(§6) must not enter code.

---

## 0. Quick overview (numbers)

| mechanism | table/channel | registered event_names | destination | retention |
|------|------|-------------|------|------|
| audit (category=audit) | `events` | 20 | events table | 365d+ |
| telemetry (category=telemetry) | `events` | 97 | events table | 90d |
| log (category=log) | `events` | 3 | events table | 30d |
| file-only (destination=file) | file log | 1 | file only (not the events table) | — |
| SSE live | Redis → frontend (not persisted) | 27 role | live projection | ephemeral |

All persistent events land in the single `events` table (`category` distinguishes
audit / telemetry / log). The four legacy mechanisms under the unified event model
(in one sentence): **audit = the category=audit part of the event river;
telemetry + log = the category=telemetry + log parts; SSE = a live projection of the
latest drops of the event river; OTel trace = the call-chain view of the event
river** (trace_id ties them together). Full treatment in
[event-system design decision](../../decisions/2026-08-04-event-system-design.md).

---

## 1. Category split

The unified model's `category` field drives retention, query permissions, and
monitoring alerts (design doc §5):

| category | what it is | destination | target retention |
|----------|--------|---------|---------|
| `audit` | business-operation facts: who did what to whom (spawn/message/task/status) | `events` | 365d+ (immutable, append-only) |
| `telemetry` | runtime observations: token, turn, exec, node, health, delivery | `events` | 90d |
| `log` | bare log lines (logger calls without event=/label=) | `events` | 30d (currently INFO+ persisted; design intent: L2 = WARNING+ only, see §7.2) |

SSE roles are not persisted and have no retention concept — live projection; OTel
spans go through the trace channel (30d).

---

## 2. Audit events (20 primary category=audit; 20 status_change with extra_categories)

**Meaning convention**: category=audit rows are append-only operation audits, one row
= one agent operation fact. `source` (who triggered: `agent:N` / `user` / `system` /
`self`) and `target_agent_id` (against whom) are the two key audit dimensions, queried
more often than payload. Payload keys other than those listed have no Pydantic model
(display-surface use; see the payload tiering rules in `shared/audit_events.py`).
Emit sites and consumers: see the comments at each emit point.

| event_name | meaning | key payload fields | retention | destination |
|------|------|-----------------|------|------|
| `spawn` | new agent born | machine, fork_from, fork_checkpoint | 365d | events |
| `fork` | agent forked from another | — | 365d | events |
| `send_message` | message sent to an agent | — | 365d | events |
| `terminate` | agent terminated | — | 365d | events |
| `restart` | agent restart initiated | — | 365d | events |
| `cancel` | in-flight turn cancelled | — | 365d | events |
| `resurrect` | terminated agent woken | — | 365d | events |
| `restart_completed` | restart finished | — | 365d | events |
| `compact` | agent context compacted | — | 365d | events |
| `report_activity` | activity report | — | 365d | events |
| `exit` | agent process exited | — | 365d | events |
| `label_change` | agent label changed | — | 365d | events |
| `skill_invoked` | skill invoked by an agent | — | 365d | events |
| `task_create` | task created | — | 365d | events |
| `task_update` | task updated | status | 365d | events |
| `report_breached` | guarantee report breached | — | 365d | events |
| `computer_action` | computer-use desktop action (executed or refused) | action, app, outcome, error, coords, path, task_id | 365d | events |
| `computer_session_start` | computer-use task session opened (first action with a task_id) | task_id, first_tool, first_action_at | 365d | events |
| `computer_session_end` | computer-use task session closed (idle timeout) | task_id, action_count, first_action_at, last_action_at, outcome | 365d | events |
| `mcp_tool_call` | MCP tool invoked through the gateway /mcp endpoint (agent_id NULL — external client) | — | 365d | events |

## 3. Telemetry events (category=telemetry, 97)

Telemetry-side event name resolution (`shared/log.py`): **explicit `event=` →
`label=` fallback → default `"log"`**. Payload = logger extra fields + `msg`
(formatted full text) + exception traceback. `(L)` marks names currently produced via
**label fallback** (no explicit event=); `(SQL)` marks writes that bypass loguru and
write SQL directly — both are annotated in the registry doc field. Emit sites and
consumers: see the comments at each emit point.

| event_name | meaning | key payload fields | family | retention | destination |
|------|------|-----------------|----|------|------|
| `status_change` | agent status transition — both telemetry (loguru) and audit (audit_events) sides emit this name | from, to | — | 90d | events |
| `frontend_interaction` | tracked frontend interaction (click / page view / settings change) | page, element, session_id, key, value | — | 90d | events |
| `llm_usage` | LLM call metering | model, calls, in_total, out_total, cache_read, reasoning, latency_ms, decode_ms, cost_usd, price_miss, price_hit, price_out, unpriced | — | 90d | events |
| `turn_end` | one turn finished | ok, duration_seconds | — | 90d | events |
| `llm_turn_aborted` | turn aborted after retries | — | LLM_ERROR | 90d | events |
| `compact_turn_aborted` | turn aborted because compaction failed | — | — | 90d | events |
| `llm_provider_error` | LLM provider failure | error_class, provider, status, error_type, fatal, billing, vendor, model | LLM_ERROR | 90d | events |
| `stream_stalled_retry` | stream stalled, retried | — | LLM_ERROR | 90d | events |
| `stream_overloaded_retry` | stream overloaded, retried | — | LLM_ERROR | 90d | events |
| `thinking_block_sanitized` | thinking block sanitized | — | — | 90d | events |
| `multiple_tool_calls_merged` | concurrent tool calls merged | — | — | 90d | events |
| `llm_cancelled` | LLM call cancelled | — | — | 90d | events |
| `exec` | execute_code succeeded | body, ok, duration_seconds | — | 90d | events |
| `exec_failed` | execute_code failed | exc_type, body | — | 90d | events |
| `exec_cancelled` | execute_code cancelled | — | — | 90d | events |
| `exec(timeout)` | historical parenthesized name (migration target) | — | — | 90d | events |
| `exec(failed)` | historical parenthesized name (migration target) | — | — | 90d | events |
| `exec(cancelled)` | historical parenthesized name (migration target) | — | — | 90d | events |
| `exec(thread-stuck)` | historical parenthesized name (migration target) | — | — | 90d | events |
| `exec_timeout` | execute_code timed out | — | — | 90d | events |
| `exec_node_timeout` | node-level timeout | — | — | 90d | events |
| `exec_thread_stuck` | exec thread stuck | — | — | 90d | events |
| `exec_thread_unreapable` | orphan exec thread survived the reap window | — | — | 90d | events |
| `host_dispatcher_subscribed` | hosted dispatcher subscribed to the inbound wake pattern | — | — | 90d | events |
| `host_dispatcher_reconnect` | hosted dispatcher's wake subscription dropped — reconnecting (wakes published while down are lost; the delivery watchdog re-publish covers them) | — | — | 90d | events |
| `host_dispatcher_bad_channel` | hosted dispatcher ignored a wake whose channel name carried no agent id | — | — | 90d | events |
| `host_turn_crashed` | a hosted turn task raised — the task is dropped and the next wake retries from the checkpoint; neighbours are unaffected | — | — | 90d | events |
| `host_agent_prepared` | the host built an agent's per-agent runtime (chat model + startup reconcile) on a cold path — carries duration_ms and a reason of cold / config_changed / evicted, so a wake that pays the cold cost is distinguishable from one that does not, and a cache thrashing on config churn is visible as reason mix | — | — | 90d | events |
| `host_started` | the hosted agent-runner finished process-scope boot and its dispatcher is live | — | — | 90d | events |
| `host_turn_uncancellable` | a hosted turn did not unwind after being cancelled — it is blocked where asyncio cannot interrupt it (a C call), so the host stopped waiting and exited. Carries the agent, how long the cancel was pending (waited_s), and the agent's real activity clock (last_active_at / idle_s from agents_meta, NOT the /api/agents field of the same name, which is MAX(inbound_messages.created_at) and goes stale during long turns — issue #183) so a slow shutdown is distinguishable from a genuine wedge. The turn resumes from its checkpoint on restart. Process mode had no equivalent because SIGKILL always lands | — | — | 90d | events |
| `node_enter` | LangGraph node entered — sink-filtered out of the events table (PR #1758); log files only | — | — | 90d | file |
| `node_exit` | LangGraph node exited | node, outcome, duration_seconds, exc_name | — | 90d | events |
| `process_exit` | agent process exited | reason, pid | — | 90d | events |
| `service_started` | gateway/daemon started | name, pid | — | 90d | events |
| `halt` | turn stopped (idle/compact/system) | body | — | 90d | events |
| `agent_restarted` | agent restarted (phase2 done) | — | — | 90d | events |
| `heartbeat_nudged` | heartbeat reminder | idle_minutes | — | 90d | events |
| `delivery_stalled` | delivery backlog | inbound_id, age_s | — | 90d | events |
| `restart_cas_lost` | restart CAS race lost | — | — | 90d | events |
| `claim_cas_lost` | claim CAS race lost — another lifecycle op owns the row | — | — | 90d | events |
| `claim_cas_lost_exit` | claim wait aborted by a lost CAS — process exiting cleanly | — | — | 90d | events |
| `idle_cas_lost` | idle-flip CAS race lost — degraded, not fatal | — | — | 90d | events |
| `boot_timing` | boot duration | — | — | 90d | events |
| `dangling_tool_use_repaired` | dangling tool_use repaired | — | — | 90d | events |
| `agent_spawned` | agent process started | spawner, forked_from | — | 90d | events |
| `agent_resurrected` | agent resurrected | — | — | 90d | events |
| `agent_terminated` | agent terminated | — | — | 90d | events |
| `agent_hibernating` | agent hibernated | — | — | 90d | events |
| `agent_swapped_in` | process swapped in | — | — | 90d | events |
| `agent_revived` | agent revived | — | — | 90d | events |
| `respawn_phase1` | restart phase 1 | — | — | 90d | events |
| `respawn_phase2_launch` | restart phase 2 launch | — | — | 90d | events |
| `launch_confirm_extended` | launch confirm extended | — | — | 90d | events |
| `launch_confirm_failed` | launch confirm failed | — | — | 90d | events |
| `launch_confirm_task_crashed` | launch confirm task crashed | — | — | 90d | events |
| `launch_force_terminated` | launch force-terminated | — | — | 90d | events |
| `launch_force_terminated_skipped` | launch force-terminate skipped | — | — | 90d | events |
| `launch_retry` | launch retried | — | — | 90d | events |
| `sdk_call` | SDK call metering | fn, duration | — | 90d | events |
| `plugin_activation` | a plugin injection surface fired (hook / wrap / prompt section) | plugin, surface, identifier, detail, model | — | 90d | events |
| `sse_drop` | SSE event dropped | kind, n | — | 90d | events |
| `event_log_drop` | event-pipeline row shed | n | — | 90d | events |
| `heartbeat_paused` | heartbeat paused | duration_s | — | 90d | events |
| `code` | LLM generated code block | body, ok, duration_seconds | — | 90d | events |
| `text` | LLM text output | — | — | 90d | events |
| `syntax_fix` | syntax repair executed | fixes | — | 90d | events |
| `inbound_reconcile` | inbound reconciliation | — | — | 90d | events |
| `screen_capture_notify_failed` | screenshot notify failed | — | — | 90d | events |
| `page_restore_alive` | page restore alive | — | — | 90d | events |
| `page_restore_reserved` | page restore reserved | — | — | 90d | events |
| `page_restore_query_failed` | page restore query failed | — | — | 90d | events |
| `page_restore_failed` | page restore failed | — | — | 90d | events |
| `page_restore_closed` | page restore closed | — | — | 90d | events |
| `db_outage_wait` | db outage wait | — | — | 90d | events |
| `db_outage_pause` | db outage pause | — | — | 90d | events |
| `db_outage_reconcile_retry` | db outage reconcile retry | — | — | 90d | events |
| `db_recovered` | db recovered | — | — | 90d | events |
| `db_pool_acquire_timeout` | db pool acquire timeout | — | — | 90d | events |
| `db_pool_acquire_slow` | db pool acquire slow | — | — | 90d | events |
| `checkpoint_write_failed` | checkpoint write failed | — | — | 90d | events |
| `pgbouncer_repaired` | pgbouncer watchdog repair | — | — | 90d | events |
| `label_generated` | label auto-generated | — | — | 90d | events |
| `label_generate_failed` | label generation failed | — | — | 90d | events |
| `label_generate_skipped` | label generation skipped | — | — | 90d | events |
| `label_generate_empty` | label generation empty | — | — | 90d | events |
| `trace` | otel span export | — | — | 90d | events |
| `idle_wake` | agent woken from idle | degraded, elapsed_s, rounds, timeout_s | — | 90d | events |
| `compact_request` | compact requested | — | — | 90d | events |
| `auto_compact` | auto-compact | — | — | 90d | events |
| `compact_reminder` | compact reminder | — | — | 90d | events |
| `history_dump` | pre-compact history dumped to workspace | — | — | 90d | events |
| `checkpoint_trim` | checkpoint trimmed | — | — | 90d | events |
| `recall_filter` | memory recall filter | body | — | 90d | events |
| `passive_recall` | passive memory recall | — | — | 90d | events |
| `silent_idle` | silent idle verdict | — | — | 90d | events |
| `last_msg` | last-message check | — | — | 90d | events |
| `gateway_latency` | gateway endpoint latency — 60s aggregate per route (p50/p95/max/count) | route, p50_ms, p95_ms, max_ms, count | — | 90d | events |

## 4. Log (bare logs, category=log)

| event_name | meaning | key payload fields | retention | destination |
|------|------|-----------------|------|------|
| `log` | bare log line | msg | 30d | events |
| `warning_resolved` | mark a warning (or class of warnings, via attributes.match) resolved | target_event_id, match, resolved_by | 30d | events |
| `error_resolved` | mark an error (or class of errors, via attributes.match) resolved | target_event_id, match, resolved_by | 30d | events |

## 5. SSE roles (live channel, not persisted, 27)

Typed Pydantic discriminators in `shared/live_events.py` (role is a Literal);
`EVENT_ADAPTER` / `SYSTEM_ROLES` / `GLOBAL_ROLES` derive from the single
`_ROLE_CLASSES` registry (R2-C). SSE is a **live projection** of the "latest drops"
of the event river — not persisted, unlike persistent events; role naming shares the
same origin as persistent events but uses an independent schema.

| role | global broadcast (/api/system) |
|------|------|
| `agent_spawned` | ✓ |
| `agent_updated` | ✓ |
| `cancelled` | — |
| `chat_delta` | — |
| `chat_start` | — |
| `code_delta` | — |
| `code_start` | — |
| `compact_done` | — |
| `compact_request` | — |
| `error` | — |
| `exec_output` | — |
| `exec_output_chunk` | — |
| `exec_start` | — |
| `inbound_arrived` | — |
| `inbound_committed` | — |
| `label_updated` | ✓ |
| `llm_done` | — |
| `notice_posted` | ✓ |
| `notice_resolved` | ✓ |
| `page_closed` | ✓ |
| `page_opened` | ✓ |
| `reasoning_delta` | — |
| `reasoning_start` | — |
| `task_created` | ✓ |
| `task_updated` | ✓ |
| `timeline_snapshot` | — |
| `token_usage` | — |

---

## 6. Naming rules (mandatory for new event_names)

Under the unified model `event_name` is a globally unique event name (OTel
`event.name`). Rules:

1. **Format: `<domain>_<action>`, lowercase snake_case**. Domain = the subject domain
   (`llm`, `exec`, `db`, `agent`, `task`, `sse`, `label`, `launch`, `respawn`,
   `checkpoint`, `stream`, `service`, `boot`, `process`, `heartbeat`...), action =
   past tense or noun (`usage`, `end`, `enter`, `exit`, `failed`, `paused`,
   `dropped`...).
   Examples: `llm_usage` ✓, `db_pool_acquire_timeout` ✓, `exec(timeout)` ✗.
2. **No bare `log`**: every logger call must carry an explicit `event=`; `label=` is a
   UI display field only and must not be reused as an event name (the fallback
   remains only for migration-period compatibility).
3. **No dynamic event_name values**: event_name must come from a static literal or an
   in-code registry constant. Dynamic dimensions (agent_id, machine, node, status)
   always go into `attributes`/payload, **never into the event_name string**.
4. **No punctuation beyond underscores**: event_name charset is `[a-z0-9_]`, no
   parentheses.
5. **One fact = one event_name, no cross-category near-duplicates**: when the same
   fact exists in both audit and telemetry, distinguish by category, do not mint a
   near-synonym name.
6. **New event_names must be registered in `EVENTS` in `shared/events/contract.py`
   first** (this document is generated from it; registration = documented), and the
   PR description cites the registry entry.
7. **Payload tiering**: only event_names whose payload fields are **branched on** by
   downstream programs get Pydantic models; display-surface payloads stay untyped
   dicts (tiering rules in `shared/audit_events.py`).

---

## 7. Noise and governance record (by priority)

### 7.1 [P0] Dynamic-label pseudo event_names — ✓ governed (W8, 2026-08-04)

Before: `status (agent N)` (5,626), `idle wake (agent N)` (2,180), `claim (agent N)`
(1,016) — label fallback spliced agent_id into the event name, exploding the event
value space (468 distinct vs ~60 static), so any stats/alerts keyed by event_name
broke.

**Governance done (W8 · PR #1348)**:
- the 3 dynamic labels in `agent/db.py` → explicit `event="status_change"` (×2,
  payload carries from/to) and `event="idle_wake"` (payload carries
  degraded/elapsed_s/rounds/timeout_s); label keeps its UI display role
  (`status-change` / `idle-wake`).
- `claim (agent N)` was already eliminated by the earlier #1186 refactor (no new
  rows since 8/1); no residue.
- existing historical rows (~75k) stay as-is; new data is all static event_name.

**Re-run verification**: the event=/label= inventory from `scan_kinds.py --repo .`
shows no dynamic concatenation left.

### 7.2 [P0] Bare logs are 50.8% (3.07M / 6.04M rows)

Open item from design doc §8.2: L2 only accepts WARNING+. Today INFO bare logs are
pure filtering burden for metrics/rollup. **Governance**: downgrade the DB sink
(INFO stays in JSONL), require explicit event=.

### 7.3 [P1] Cross-category duplicates / naming inconsistencies — ✓ governed (W8, 2026-08-04)

- `compact` (audit) vs the agent_events label fallback `compact` — **governed**:
  agent_events now uses `compact_request`, audit keeps `compact`.
- `resurrect` (audit) vs `agent_resurrected` (telemetry) overlap semantically —
  **coordinated**: names were already distinct, kept apart by category.
- Parenthesized names `llm(cancelled)` / `exec(timeout)` → `llm_cancelled` /
  `exec_timeout` — **governed**.
- Hyphenated names `recall-filter` / `checkpoint-trim` / `compact-reminder` →
  `recall_filter` / `checkpoint_trim` / `compact_reminder` — **governed**.
- `llm-provider-error` and `llm_provider_error` are two names for one fact —
  **merged**: the label display name is unified as `llm_provider_error`, one event
  name.

### 7.4 [P2] Historical event_names with no producer

`report_activity` (5,274 rows) and `report_breached` (14 rows) have no producer left
in code, but the schema comment and the self-evolution collector still reference
them. Confirm keep/retire when the unified model lands.

### 7.5 [Resolved] skill_invoked parse/injection noise

Both historical sources eliminated: `invocation_depth=prompt_injected` is no longer
written; node parsing no longer records it since 2026-08-06 — the event is only
produced when the SKILL.md body is first consumed (`help()` / `__doc__`), so the
loaded semantics = the body was actually read.

### 7.6 [Settled] Final event_name-category caliber (2026-08-05, tracker #762/#763)

The `EVENTS` registry in `shared/events/contract.py` (R2-C) is the final caliber for
event_name and category (telemetry 90d / log 30d); `_TELEMETRY_KINDS` in
`shared/telemetry.py` is a derived projection, no longer hand-maintained:

- **`text`, `syntax_fix` → telemetry**: the whitelist previously missed these two
  label-fallback event_names, so live data landed in log (30d). Now whitelisted +
  historical rows (~103k) backfilled to telemetry by migration
  `20260805T083741_kind-category-final`.
- **`agent_restarted` → telemetry** (#763): whitelisted in W8; live and migration
  agree.
- **Historical/dead event_names stay log**: `claim`, `code(cancelled)`,
  `exec(interrupt)`, `stream_corrupted_retry`,
  `stream_corrupted_or_stalled_retry`, `watcher_resume_recovery`,
  `report_overdue_nudged`, `report_paused` — no producer, no consumer, not
  whitelisted (natural retirement at 30d).
- **`recall_filter` panel fixed**: `ava_builtins/plugins/ava_memory/metrics.py`
  previously queried the old spelling `recall-filter` (0 rows since the W8 rename),
  now queries `recall_filter` + telemetry.

---

## 8. Inventory method and coverage (reproducible)

1. **Registry**: the `EVENTS` in `shared/events/contract.py` is the single source of
   truth; this document is generated by `scripts/gen_event_registry.py` (the
   pre-commit `events-registry-fresh` hook verifies no drift).
2. **Code-literal cross-check**: `python shared/events/scan_kinds.py` — scans
   production Python code (excluding tests/worktrees/node_modules) for `event=`,
   `label=`, `event_type=`, SSE roles, emits four inventories, and cross-checks them
   bidirectionally against the registry (`tests/test_lint_event_kinds.py`).
3. **DB-measured distribution** (optional): `python shared/events/scan_kinds.py --db-url "$AVA_DB_URL"`
   — appends the events table's event_name distribution and bare-log share (all
   read-only SELECTs).
4. **Coverage statement**: this document covers every registered event_name (audit +
   telemetry + log + destination=file + SSE role). Not covered: test-fixture
   self-made names like `evt`/`my_event`/`some_warning` (non-production events).

**CI wiring**: `tests/test_lint_event_kinds.py` is a three-way guard — every static
`event=` literal in code must be registered (in `EVENTS`), every registry entry must
have a producer (event=/label=/SQL/dynamic inventories), and this document must match
the generator output (drift = red). Emit-side fail-fast additionally raises
`ValueError` for unregistered names.

**When to regenerate**: run the generator before any PR that adds/renames an
event_name or SSE role; the pre-commit hook blocks drift.
