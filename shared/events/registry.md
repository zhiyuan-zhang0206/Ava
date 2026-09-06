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

| mechanism | table/channel | registered event_names | destination |
|------|------|-------------|------|
| audit (category=audit) | `events` | 23 | events table |
| telemetry (category=telemetry) | `events` | 157 | events table |
| log (category=log) | `events` | 9 | events table |
| file-only (destination=file) | file log | 1 | file only (not the events table) |
| SSE live | Redis → frontend (not persisted) | 28 role | live projection |

All persistent events land in the single `events` table (`category` distinguishes
audit / telemetry / log). The four legacy mechanisms under the unified event model
(in one sentence): **audit = the category=audit part of the event river;
telemetry + log = the category=telemetry + log parts; SSE = a live projection of the
latest drops of the event river; OTel trace = the call-chain view of the event
river** (trace_id ties them together). Full treatment in
[event-system design decision](../../decisions/2026-08-04-event-system-design.md).

---

## 1. Category split

The unified model's `category` field drives query permissions and
monitoring alerts (design doc §5):

| category | what it is | destination |
|----------|--------|---------|
| `audit` | business-operation facts: who did what to whom (spawn/message/task/status) | `events` |
| `telemetry` | runtime observations: token, turn, exec, node, health, delivery | `events` |
| `log` | bare log lines (logger calls without event=/label=) | `events` |

SSE roles are not persisted and have no retention concept — live projection; OTel
spans go through the trace channel (30d).

---

## 2. Audit events (23 primary category=audit; 23 status_change with extra_categories)

**Meaning convention**: category=audit rows are append-only operation audits, one row
= one agent operation fact. `source` (who triggered: `agent:N` / `user` / `system` /
`self`) and `target_agent_id` (against whom) are the two key audit dimensions, queried
more often than payload. Payload keys other than those listed have no Pydantic model
(display-surface use; see the payload tiering rules in `shared/audit_events.py`).
Emit sites and consumers: see the comments at each emit point.

| event_name | meaning | tier | key payload fields | destination |
|------|------|------|-----------------|------|
| `spawn` | new agent born | business | machine, fork_from, fork_checkpoint | events |
| `fork` | agent forked from another | business | — | events |
| `send_message` | message sent to an agent | business | — | events |
| `terminate` | agent terminated | business | — | events |
| `restart` | agent restart initiated | business | — | events |
| `cancel` | in-flight turn cancelled | business | — | events |
| `resurrect` | terminated agent woken | business | — | events |
| `restart_completed` | restart finished | business | — | events |
| `compact` | agent context compacted | business | — | events |
| `circuit_breaker` | heartbeat circuit breaker opened — a permanent provider rejection stopped heartbeat re-fires (context_overflow reason arms the forced-compact self-rescue) | business | — | events |
| `report_activity` | activity report | business | — | events |
| `exit` | agent process exited | business | — | events |
| `label_change` | agent label changed | business | — | events |
| `skill_invoked` | skill invoked by an agent | business | — | events |
| `task_create` | task created | business | — | events |
| `task_update` | task updated | business | status | events |
| `report_breached` | guarantee report breached | business | — | events |
| `computer_action` | computer-use desktop action (executed or refused) | business | action, app, outcome, error, coords, path, task_id | events |
| `env_write` | official .env config write (actor and keys; values never recorded) | business | — | events |
| `env_unauthorized_write` | out-of-band .env modification detected (no official write recorded) | anomaly | — | events |
| `computer_session_start` | computer-use task session opened (first action with a task_id) | business | task_id, first_tool, first_action_at | events |
| `computer_session_end` | computer-use task session closed (idle timeout) | business | task_id, action_count, first_action_at, last_action_at, outcome | events |
| `mcp_tool_call` | MCP tool invoked through the gateway /mcp endpoint (client-scoped, args redacted) | business | — | events |

## 3. Telemetry events (category=telemetry, 157)

Telemetry-side event name resolution (`shared/log.py`): **explicit `event=` →
`label=` fallback → default `"log"`**. Payload = logger extra fields + `msg`
(formatted full text) + exception traceback. `(L)` marks names currently produced via
**label fallback** (no explicit event=); `(SQL)` marks writes that bypass loguru and
write SQL directly — both are annotated in the registry doc field. Emit sites and
consumers: see the comments at each emit point.

| event_name | meaning | tier | key payload fields | family | destination |
|------|------|------|-----------------|----|------|
| `status_change` | agent status transition — both telemetry (loguru) and audit (audit_events) sides emit this name | noise | from, to | — | events |
| `frontend_interaction` | tracked frontend interaction (click / page view / settings change) | noise | page, element, session_id, key, value | — | events |
| `llm_usage` | LLM call metering | observation | model, calls, in_total, out_total, cache_read, reasoning, latency_ms, decode_ms, cost_usd, price_miss, price_hit, price_out, unpriced, task_id, usage_kind, source | — | events |
| `turn_end` | one turn finished | observation | ok, duration_seconds | — | events |
| `llm_turn_aborted` | turn aborted after retries | anomaly | — | LLM_ERROR | events |
| `compact_turn_aborted` | turn aborted because compaction failed | anomaly | — | — | events |
| `llm_provider_error` | LLM provider failure | anomaly | error_class, provider, status, error_type, fatal, billing, vendor, model | LLM_ERROR | events |
| `stream_stalled_retry` | stream stalled, retried | anomaly | — | LLM_ERROR | events |
| `stream_overloaded_retry` | stream overloaded, retried | anomaly | — | LLM_ERROR | events |
| `thinking_block_sanitized` | thinking block sanitized | noise | — | — | events |
| `multiple_tool_calls_merged` | concurrent tool calls merged | observation | — | — | events |
| `llm_cancelled` | LLM call cancelled | anomaly | — | — | events |
| `exec` | execute_code succeeded | observation | body, ok, duration_seconds | — | events |
| `exec_failed` | execute_code failed | anomaly | exc_type, body | — | events |
| `plugin_load_failed` | enabled plugin skipped because it failed to load (fail-soft) | anomaly | plugin, error | — | events |
| `exec_envelope` | exec envelope transfer cost (size + serialize time) — request snapshot / result delta | observation | envelope, op, size_bytes, serialize_ms | — | events |
| `exec_child_boot` | exec child bootstrap duration before agent-authored code | noise | duration_ms | — | events |
| `compaction_completed` | applied context compaction size reduction and completed count | noise | compact_kind, compactions, history_chars, summary_chars, summary_history_ratio | — | events |
| `exec_cancelled` | execute_code cancelled | anomaly | — | — | events |
| `exec(timeout)` | historical parenthesized name (migration target) | anomaly | — | — | events |
| `exec(failed)` | historical parenthesized name (migration target) | anomaly | — | — | events |
| `exec(cancelled)` | historical parenthesized name (migration target) | anomaly | — | — | events |
| `exec(thread-stuck)` | historical parenthesized name (migration target) | anomaly | — | — | events |
| `exec_timeout` | execute_code timed out | anomaly | — | — | events |
| `exec_node_timeout` | node-level timeout | anomaly | — | — | events |
| `exec_subprocess_killed` | exec child survived the signal grace period and was SIGKILLed | anomaly | pid, grace | — | events |
| `host_stale_running_settled` | hosted boot settle restored rows a previous host instance left running without a task (crash / kill -9); carries n = rows settled | noise | — | — | events |
| `host_dispatcher_subscribed` | hosted dispatcher subscribed to the inbound wake pattern | noise | — | — | events |
| `host_dispatcher_reconnect` | hosted dispatcher's wake subscription dropped — reconnecting (wakes published while down are lost; the delivery watchdog re-publish covers them) | noise | — | — | events |
| `host_dispatcher_scan_failed` | hosted dispatcher's durable pending scan failed; the wake subscription remains open and attributes carry the next scan backoff_s | anomaly | backoff_s | — | events |
| `host_dispatcher_restart_required` | hosted dispatcher could not unwind a stale turn — exiting for supervisor recovery | anomaly | — | — | events |
| `host_dispatcher_bad_channel` | hosted dispatcher ignored a wake whose channel name carried no agent id | anomaly | — | — | events |
| `host_config_rejected` | a hosted wake was consumed without a turn because the agent's stored model config cannot build (unknown model or missing provider key) — logged once per stored config state (fingerprint); the pending inbound is kept until the overlay is fixed | anomaly | — | — | events |
| `host_turn_crashed` | a hosted turn task raised — the task is dropped and the next wake retries from the checkpoint; neighbours are unaffected. Carries exception_type, plus config_fingerprint when the stored config was read before the failure | anomaly | — | — | events |
| `host_agent_prepared` | the host built an agent's per-agent runtime (chat model + startup reconcile) on a cold path — carries duration_ms and a reason of cold / config_changed / evicted, so a wake that pays the cold cost is distinguishable from one that does not, and a cache thrashing on config churn is visible as reason mix | noise | — | — | events |
| `host_started` | the hosted agent-runner finished process-scope boot and its dispatcher is live | noise | — | — | events |
| `host_stdout_log_rotated` | the hosted daemon rotated its raw stdout transcript at the size ceiling (task #2356) — carries size and ceiling; a crash storm shows up as repeated rotation events instead of an unbounded file | noise | — | — | events |
| `host_turn_uncancellable` | a hosted turn did not unwind after being cancelled — it is blocked where asyncio cannot interrupt it (a C call), so the host stopped waiting and exited. Carries the agent, how long the cancel was pending (waited_s), and the agent's real activity clock (last_active_at / idle_s from agents_meta, NOT the /api/agents field of the same name, which is MAX(inbound_messages.created_at) and goes stale during long turns — issue #183) so a slow shutdown is distinguishable from a genuine wedge. The turn resumes from its checkpoint on restart. Process mode had no equivalent because SIGKILL always lands | anomaly | — | — | events |
| `host_turn_stall_timeout` | the hosted stall guard aborted a graph.ainvoke whose turn clock (agent/_turn_progress.py: node enters + completed LLM steps) was silent past AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS (turn activity = node enter, completed LLM step, streamed chunk) — the turn-level injection guard of task #2417. The invocation was cancelled and unwound; the row settles to idling; the next wake resumes from the checkpoint | anomaly | — | — | events |
| `host_turn_stall_uncancellable` | a stalled invocation that had been cancelled for the bounded unwind window REFUSED to unwind (blocked where asyncio cannot interrupt it — a C call). The host cannot fix this in-process: it signals a daemon restart so the supervisor recovers the turn from its checkpoint | anomaly | — | — | events |
| `host_turn_stall_aborted` | a hosted turn task ended after its no-progress abort: the invocation unwound and was dropped; the runtime was discarded by run_turn, so the next wake re-runs the startup reconcile before resuming from the checkpoint | anomaly | — | — | events |
| `host_turn_stall_detected` | the hosted dispatcher's durable scan found an in-flight turn whose turn-progress clock (agent/_turn_progress.py: node enters, completed LLM steps, streamed LLM chunks) has been silent past the wedged budget while NO pending inbound exists — the turn-level fake-alive shape (process alive, turn dead) that pending-row and pid-based detectors cannot see. The turn task is cancelled and the agent rescheduled; a turn that refuses to unwind instead escalates to a daemon restart | anomaly | — | — | events |
| `node_enter` | LangGraph node entered — sink-filtered out of the events table (PR #1758); log files only | noise | — | — | file |
| `node_exit` | LangGraph node exited | noise | count, nodes | — | events |
| `process_exit` | agent process exited | noise | reason, pid | — | events |
| `service_started` | gateway/daemon started | noise | name, pid | — | events |
| `halt` | turn stopped (idle/compact/system) | noise | body | — | events |
| `agent_restarted` | agent restarted (phase2 done) | observation | — | — | events |
| `restart_handoff_host_unhealthy` | hosted restart ownership could not transfer: agent-host is unhealthy; row left restarting for retry | anomaly | — | — | events |
| `heartbeat_nudged` | heartbeat reminder | noise | idle_minutes | — | events |
| `heartbeat_backoff_raised` | no-op nudge backoff level raised | noise | level, interval_seconds | — | events |
| `heartbeat_backoff_reset` | nudge backoff reset by real inbound or pause | noise | previous_level, reason | — | events |
| `ci_usage_daily` | daily CI-minute reconciliation totals (C9) | observation | day, window_start, window_end, runs, attributed_runs, unattributed_runs, total_minutes, attributed_minutes, linux_minutes, macos_minutes, appended_runs, est_usd | — | events |
| `task_reminder_digest` | overdue-task owner digest | noise | owner_id, task_count, task_ids | — | events |
| `task_escalation` | stalled-task escalation | observation | owner_id, task_count, task_ids, leg | — | events |
| `delivery_stalled` | delivery backlog | anomaly | inbound_id, age_s | — | events |
| `loki_write_path_probe_failed` | Loki write-path probe failed | anomaly | consecutive_failures, reason | — | events |
| `delivery_poisoned` | delivery backlog — permanently-failing inbound poisoned (dispatch cap reached) | anomaly | inbound_id, dispatch_count, age_s | — | events |
| `delivery_wake_suppressed` | automatic delivery wakes suppressed after repeated resurrection failures | anomaly | consecutive_failures, suppress_seconds, suppress_count, reason | — | events |
| `claim_cas_lost` | claim CAS race lost — another lifecycle op owns the row | anomaly | — | — | events |
| `claim_cas_lost_exit` | claim wait aborted by a lost CAS — process exiting cleanly | anomaly | — | — | events |
| `idle_cas_lost` | idle-flip CAS race lost — degraded, not fatal | anomaly | — | — | events |
| `boot_timing` | boot duration | noise | — | — | events |
| `dangling_tool_pairing_repaired` | dangling tool pairing repaired | anomaly | — | — | events |
| `agent_spawned` | agent process started | observation | spawner, forked_from | — | events |
| `agent_resurrected` | agent resurrected | observation | — | — | events |
| `agent_terminated` | agent terminated | observation | — | — | events |
| `agent_revived` | agent revived | noise | — | — | events |
| `respawn_phase1` | restart phase 1 | noise | — | — | events |
| `respawn_phase2_launch` | restart phase 2 launch | noise | — | — | events |
| `launch_confirm_extended` | launch confirm extended | noise | — | — | events |
| `launch_confirm_failed` | launch confirm failed | anomaly | — | — | events |
| `agent_boot_failed` | agent boot failed (process exits; crash-loop budget applies) | anomaly | model, error_type, error | — | events |
| `launch_confirm_task_crashed` | launch confirm task crashed | anomaly | — | — | events |
| `launch_force_terminated` | launch force-terminated | anomaly | — | — | events |
| `launch_force_terminated_skipped` | launch force-terminate skipped | noise | — | — | events |
| `launch_retry` | launch retried | observation | — | — | events |
| `sdk_call` | SDK call metering | noise | fn, duration, sample_rate | — | events |
| `plugin_activation` | a plugin injection surface fired (hook / wrap / prompt section) | noise | plugin, surface, identifier, detail, model | — | events |
| `sse_drop` | SSE event dropped | anomaly | kind, n | — | events |
| `event_log_drop` | event-pipeline row shed | anomaly | n | — | events |
| `heartbeat_paused` | heartbeat paused | observation | duration_s | — | events |
| `code` | LLM generated code block | noise | body, ok, duration_seconds | — | events |
| `text` | LLM text output | noise | — | — | events |
| `syntax_fix` | syntax repair executed | noise | fixes | — | events |
| `inbound_reconcile` | inbound reconciliation | noise | — | — | events |
| `screen_capture_notify_failed` | screenshot notify failed | anomaly | — | — | events |
| `page_restore_alive` | page restore alive | noise | — | — | events |
| `page_restore_reserved` | page restore reserved | noise | — | — | events |
| `page_restore_query_failed` | page restore query failed | anomaly | — | — | events |
| `page_restore_failed` | page restore failed | anomaly | — | — | events |
| `page_restore_closed` | page restore closed | noise | — | — | events |
| `page_restore_notified` | page restore notified | noise | — | — | events |
| `db_outage_wait` | db outage wait | anomaly | — | — | events |
| `db_outage_pause` | db outage pause | anomaly | — | — | events |
| `db_outage_reconcile_retry` | db outage reconcile retry | anomaly | — | — | events |
| `db_recovered` | db recovered | anomaly | — | — | events |
| `db_pool_acquire_timeout` | db pool acquire timeout | anomaly | — | — | events |
| `db_pool_acquire_slow` | db pool acquire slow | anomaly | — | — | events |
| `checkpoint_write_failed` | checkpoint write failed | anomaly | — | — | events |
| `pgbouncer_repaired` | pgbouncer watchdog repair | anomaly | — | — | events |
| `editable_pth_repaired` | poisoned editable-install pointer repaired to the prod source root | anomaly | — | — | events |
| `editable_direct_url_repaired` | poisoned editable-install direct_url repaired to the prod source root | anomaly | — | — | events |
| `exec_editable_install_poisoned` | poisoned editable install repaired before an exec child spawn | anomaly | — | — | events |
| `source_tree_reset` | prod source checkout reset to the installed commit / cleaned of untracked files | anomaly | — | — | events |
| `label_generated` | label auto-generated | noise | — | — | events |
| `label_generate_failed` | label generation failed | anomaly | — | — | events |
| `label_generate_skipped` | label generation skipped | noise | — | — | events |
| `label_generate_empty` | label generation empty | noise | — | — | events |
| `label_generate_rejected` | label generation rejected as not a label | noise | — | — | events |
| `label_generate_retired` | label generation given up on after repeated failures | noise | — | — | events |
| `trace` | otel span export | noise | — | — | events |
| `idle_wake` | agent woken from idle | noise | degraded, elapsed_s, rounds, timeout_s, wake_state | — | events |
| `wake_degraded` | RedisInboundListener wake path degraded (instant pub/sub wake off) | anomaly | — | — | events |
| `wake_restored` | RedisInboundListener wake path recovered (clean consume restored instant wake) | noise | — | — | events |
| `compact_request` | compact requested | noise | — | — | events |
| `auto_compact` | auto-compact | noise | — | — | events |
| `compact_reminder` | compact reminder | noise | — | — | events |
| `circuit_breaker_open` | heartbeat circuit breaker opened | noise | — | — | events |
| `circuit_breaker_closed` | heartbeat circuit breaker closed | noise | — | — | events |
| `circuit_breaker_compact` | forced overflow compact fired by the open breaker | noise | — | — | events |
| `heartbeat_circuit_open` | heartbeat consumed while the breaker is open | noise | — | — | events |
| `emergency_compact` | emergency compaction (overflow self-rescue) | noise | — | — | events |
| `respawn_breaker_open` | watchdog respawn circuit breaker opened — repeated failed respawns held until a probe-alive round | anomaly | — | — | events |
| `schedule_stalled` | enabled non-completed schedule has had no live session for more than two hours | anomaly | schedule_id, status, stalled_seconds | — | events |
| `history_dump` | pre-compact history dumped to workspace | noise | — | — | events |
| `checkpoint_trim` | checkpoint trimmed | noise | — | — | events |
| `recall_filter` | memory recall filter | noise | body, query_hmac_sha256, picked_paths | — | events |
| `passive_recall` | passive memory recall | noise | search_ms, filter_ms | — | events |
| `hook_timing` | hook-runner pass — per-hook wall durations, attributing a slow before_llm / before_exec node to its hooks from events alone | noise | hook_ms | — | events |
| `silent_idle` | silent idle cost-boundary verdict | noise | output_tokens, cumulative_output_tokens, estimated_cost_usd, halted | — | events |
| `llm_retry` | LLM retry sequence completion | observation | outcome, duration_seconds | — | events |
| `last_msg` | last-message check | noise | — | — | events |
| `gateway_latency` | gateway endpoint latency — 60s aggregate per route (p50/p95/p99/max/count) | noise | route, p50_ms, p95_ms, p99_ms, max_ms, count | — | events |
| `sse` | gateway SSE lifecycle — active connections by mode plus open/close counters | noise | mode, active_connections, opened, closed | — | events |
| `gateway_process` | gateway process CPU, resident memory, and open file descriptors (60s sample) | noise | cpu_percent, rss_bytes, fd_count | — | events |
| `gateway_event_loop` | gateway event-loop maximum callback lag and slow ticks (60s window) | noise | lag_ms, slow_ticks | — | events |
| `auth401_rejected` | gateway auth-401 rejections in the 60s window (aggregate count) | noise | count | — | events |
| `agent_registry` | agent registry max id — the agents-table high-water mark (absolute state, 60s sample) | noise | max_id | — | events |
| `memory_search_stats` | memory search store rows + last save duration (absolute state, 60s sample) | noise | rows, last_save_seconds | — | events |
| `watchdog_tick` | watchdog completed one full healthcheck and reconcile round | noise | last_tick_timestamp_seconds | — | events |
| `pitr_remote_inventory` | PITR remote object inventory (backend-scoped absolute object and byte state) | noise | backend, object_count, bytes | — | events |
| `recovery_drill_failed` | scheduled logical dump or PITR recovery proof failed | anomaly | drill, detail | — | events |
| `telemetry_read_stale` | read-side telemetry staleness detected — heartbeat older than threshold | anomaly | source, signal, threshold_s, age_s, action, reason | — | events |
| `telemetry_read_recovered` | read-side telemetry heartbeat recovered | observation | source, signal, stale_duration_s | — | events |
| `otlp_backend_disabled` | OTLP backend disabled for this process (init failure / collector unreachable); retry scheduled | anomaly | reason, endpoint | — | events |
| `otlp_backend_recovered` | OTLP backend brought up after a disabled episode (periodic retry) | observation | endpoint, disabled_s | — | events |
| `loki_query_budget` | local Loki query-admission transition and capacity metrics | noise | outcome, active, queued, high_water, wait_ms, acquired, queue_full, wait_timeout | — | events |
| `prom_query_budget` | local Prometheus query-admission transition and capacity metrics | noise | outcome, active, queued, high_water, wait_ms, acquired, queue_full, wait_timeout | — | events |
| `warning_resolved` | class-level warning dismissal marker (legacy target-event attributes remain accepted) | anomaly | target_event_id, match, resolved_by, category, level, event_name, source, agent_id, dismissed_by, note | — | events |
| `error_resolved` | class-level error/critical dismissal marker (legacy target-event attributes remain accepted) | anomaly | target_event_id, match, resolved_by, category, level, event_name, source, agent_id, dismissed_by, note | — | events |
| `warning_reopened` | class-level warning dismissal reopened manually or by the burst safety valve | anomaly | category, level, event_name, source, agent_id, dismissed_by, note, reopened_by, triggered_by_count | — | events |
| `error_reopened` | class-level error/critical dismissal reopened manually or by the burst safety valve | anomaly | category, level, event_name, source, agent_id, dismissed_by, note, reopened_by, triggered_by_count | — | events |
| `resolution_status` | absolute unresolved + dismissed warning/error class counts over the daemon's fixed six-hour window | noise | unresolved_warnings, unresolved_errors, dismissed_warnings, dismissed_errors, window | — | events |
| `checkpoint_table_sizes` | checkpoint table physical sizes and live row counts (hourly + after each blob vacuum run) | observation | blobs_bytes, checkpoints_bytes, writes_bytes, blobs_live, checkpoints_live, writes_live | — | events |
| `gate_auth_probe_failed` | gate auth probe failed — carries the classification (auth/timeout/network/application) and exception shape | anomaly | category, exception_type, exception_value, status, latency_ms | — | events |
| `archive_fetch_degraded` | frozen Loki archive read degraded (lock-wait skip or failed scan) | anomaly | route, reason | — | events |

## 4. Log (bare logs, category=log)

| event_name | meaning | tier | key payload fields | destination |
|------|------|------|-----------------|------|
| `log` | bare log line | noise | msg | events |
| `loki_query_failed` | a Loki HTTP query failed (timeout / disconnect / non-2xx) — carries the request shape | anomaly | endpoint, duration_s, error, window_from, window_to, query | events |
| `prom_query_failed` | a Prometheus HTTP query failed (timeout / disconnect / non-2xx) — carries the request shape | anomaly | endpoint, duration_s, error, query | events |
| `page_serve_dir_missing` | a served page directory disappeared; emitted on degradation and auto-close | anomaly | agent_id, key, name, serve_dir, port | events |
| `page_ttl_expired` | the gateway TTL reaper terminalized a page row whose expires_at passed; attributes carry agent_id, name, page_id | observation | — | events |
| `page_language_lookup_failed` | the gateway could not read the page copy language from user_settings (DB failure) and fell back to the default; attributes carry exc_type, exc_message | anomaly | — | events |
| `page_proxy_502` | the gateway reverse proxy could not reach a registered page server; attributes carry trace_id, agent_id, page, host, port, exc_type, exc_message | anomaly | — | events |
| `page_proxy_504` | the gateway reverse proxy timed out dialing a registered page server; attributes carry trace_id, agent_id, page, host, port, exc_type, exc_message | anomaly | — | events |
| `shell_ttl_expired` | the gateway TTL reaper killed a persistent shell whose declared TTL passed; attributes carry agent_id, session_id, mode | observation | — | events |

## 5. SSE roles (live channel, not persisted, 28)

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
| `cluster_update_started` | ✓ |
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
event_name and category; `_TELEMETRY_KINDS` in
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
