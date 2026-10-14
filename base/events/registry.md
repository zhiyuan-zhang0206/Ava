# Ava event name registry

> This document is **generated**: produced by `scripts/codegen/gen_event_registry.py` from
> the `EVENTS` registry in `base/events/contract.py`, merged from the domain modules of
> `base/events/declarations/` (since 2026-08-08, R2-C).
> Hand edits are flagged as drift by the pre-commit `events-registry-fresh` hook.
> Inventory method and history: see §8.

> **Terminology (aligned to the OTel 2026 model on 2026-08-06, task #898)**: `kind` is
> the old name — in the unified event model every event is a named LogRecord, and the
> name field is OTel `event.name` (code and the DB column are unified as
> `event_name`). Each entry in this document = one `event_name` value; the SSE-side
> discriminator is called `role` (§5, live projection, not persisted).

**The registry is the single source of truth for event_name** (the `EVENTS` in
`base/events/contract.py`). A new event = one `EventSpec` in its domain module under
`base/events/declarations/` (`telemetry.emit` fails fast on unregistered names); the tables in this document are
generated from it and never hand-synced. event_names that violate the naming rules
(§6) must not enter code.

---

## 0. Quick overview

| mechanism | table/channel | destination |
|------|------|------|
| audit (category=audit) | `events` | event stream |
| telemetry (category=telemetry) | `events` | event stream |
| log (category=log) | `events` | event stream |
| file-only (destination=file) | file log | file only (not the stream) |
| SSE live | Redis → frontend (not persisted) | live projection |

All persistent events land in the unified event stream (`category` distinguishes
audit / telemetry / log). The four legacy mechanisms under the unified event model
(in one sentence): **audit = the category=audit part of the event river;
telemetry + log = the category=telemetry + log parts; SSE = a live projection of the
latest drops of the event river; OTel trace = the call-chain view of the event
river** (trace_id ties them together). Full treatment in
[event-system design decision](../../docs/decisions/observability/events/2026-08-04-event-system-design.md).

---

## 1. Category split

The unified model's `category` field drives query permissions and
monitoring alerts (design doc §5):

| category | what it is | destination |
|----------|--------|---------|
| `audit` | business-operation facts: who did what to whom (spawn/message/task/status) | `events` |
| `telemetry` | runtime observations: token, turn, exec, node, health, delivery | `events` |
| `log` | bare log lines (logger calls without event=) | `events` |

SSE roles are not persisted and have no retention concept — live projection; OTel
spans go through the trace channel (30d).

---

## 2. Audit events

**Meaning convention**: category=audit rows are append-only operation audits, one row
= one agent operation fact. `source` (who triggered: `agent:N` / `user` / `system` /
`self`) and `target_agent_id` (against whom) are the two key audit dimensions, queried
more often than payload. Payload keys other than those listed have no Pydantic model
(display-surface use; see the payload tiering rules in `base/telemetry/audit_events.py`).
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
| `compact` | agent context compacted | business | — | events |
| `circuit_breaker` | heartbeat circuit breaker opened — a permanent provider rejection stopped background model re-fires until the cause is corrected | business | — | events |
| `exit` | agent process exited | business | — | events |
| `label_change` | agent label changed | business | — | events |
| `skill_invoked` | skill invoked by an agent | business | — | events |
| `task_create` | task created | business | — | events |
| `task_update` | task updated | business | status | events |
| `computer_action` | computer-use desktop action (executed or refused) | business | action, app, outcome, error, coords, path, task_id | events |
| `env_write` | official .env config write (actor and keys; old/new values for non-sensitive fields stay in the local record; sensitive values never recorded) | business | — | events |
| `env_unauthorized_write` | out-of-band .env modification detected (no official write recorded) | anomaly | — | events |
| `computer_session_start` | computer-use task session opened (first action with a task_id) | business | task_id, first_tool, first_action_at | events |
| `computer_session_end` | computer-use task session closed (idle timeout) | business | task_id, action_count, first_action_at, last_action_at, outcome | events |
| `mcp_tool_call` | MCP tool invoked through the gateway /mcp endpoint (client-scoped, args redacted) | business | — | events |

## 3. Telemetry events (category=telemetry)

Telemetry-side event name resolution (`base/telemetry/process_logging.py`): **explicit `event=` →
default `"log"`**. Payload = logger extra fields + `msg`
(formatted full text) + exception traceback. `(SQL)` marks writes that bypass loguru and
write SQL directly — annotated in the registry doc field. Emit sites and
consumers: see the comments at each emit point.

**Stored in `telemetry_events`**: the `stored` column is `EventSpec.persist` — a Postgres
reader queries the event by name. An event without it is stored only at warning level or
above (or when its name is unregistered); otherwise it stays in the JSONL mirror and Loki
(`base/telemetry/event_store.py: is_persisted`).

| event_name | meaning | tier | key payload fields | family | destination | stored |
|------|------|------|-----------------|----|------|----|
| `plugin_load_failed` | enabled plugin skipped because it failed to load (fail-soft) | anomaly | plugin, error | — | events | — |
| `heartbeat_nudged` | heartbeat reminder | noise | idle_minutes | — | events | — |
| `heartbeat_backoff_raised` | no-op nudge backoff level raised | noise | level, interval_seconds | — | events | — |
| `heartbeat_backoff_reset` | nudge backoff reset by real inbound or pause | noise | previous_level, reason | — | events | — |
| `machine_probe_failed` | agent-runner machine unreachable — one event per failed liveness probe | anomaly | machine, consecutive_failures | — | events | — |
| `dangling_tool_pairing_repaired` | dangling tool pairing repaired | anomaly | — | — | events | — |
| `delta_read_compat` | delta-written checkpoint messages reconstructed for a plain reader (task #3180 transition layer) | noise | — | — | events | — |
| `sdk_call` | SDK call metering | noise | fn, duration, sample_rate, detail | — | events | — |
| `sdk_sampling_refresh_unfinished` | SDK sampling refresh worker retained after the finite shutdown wait | anomaly | — | — | events | — |
| `plugin_activation` | a plugin injection surface fired (hook / wrap / prompt section) | noise | plugin, surface, identifier, detail, model | — | events | ✓ |
| `heartbeat_paused` | heartbeat paused | observation | duration_s | — | events | ✓ |
| `shell_ttl_renewed` | shell TTL deadline renewed | observation | session_id, ttl_s, prev_expires_at, new_expires_at | — | events | — |
| `screen_capture_notify_failed` | screenshot notify failed | anomaly | — | — | events | — |
| `delta_message_suffix` | message history write-body transfer and retained suffix counts; excludes snapshot seed | noise | thread_id, checkpoint_ns, checkpoint_id, candidate_writes, fetched_rows, fetched_bytes, body_batches, retained_writes, reset_found | — | events | — |
| `db_pool_acquire_timeout` | db pool acquire timeout | anomaly | — | — | events | — |
| `db_pool_acquire_slow` | db pool acquire slow | anomaly | name, elapsed, slot_wait_ms, check_ms, check_attempts, pool_size, pool_available, requests_waiting, connections_errors | — | events | — |
| `checkpoint_write_failed` | checkpoint write failed | anomaly | — | — | events | — |
| `trace` | otel span export | noise | — | — | events | — |
| `history_dump` | pre-compact history dumped to workspace | noise | — | — | events | — |
| `passive_recall` | passive memory recall | noise | body, search_ms, injected | — | events | ✓ |
| `skill_recall` | skill recall | noise | candidates, offered | — | events | ✓ |
| `hook_timing` | hook-runner pass — per-hook wall durations, attributing a slow before_llm / before_exec node to its hooks from events alone | noise | hook_ms | — | events | — |
| `last_msg` | last-message check | noise | — | — | events | — |
| `status_change` | agent status transition — both telemetry (loguru) and audit (audit_events) sides emit this name | noise | from, to | — | events | — |
| `delivery_stalled` | delivery backlog | anomaly | inbound_id, age_s | — | events | ✓ |
| `exec` | execute_code succeeded | observation | body, ok, duration_seconds | — | events | ✓ |
| `exec_failed` | execute_code failed | anomaly | exc_type, body | — | events | ✓ |
| `exec_envelope` | exec envelope transfer cost (size + serialize time) — request snapshot / result delta | observation | envelope, op, size_bytes, serialize_ms | — | events | — |
| `exec_child_boot` | exec child bootstrap duration before agent-authored code | noise | duration_ms | — | events | — |
| `exec_child_boot_failed` | exec child crashed before running the agent's code (bootstrap-class failure) | anomaly | exc_type, exc_msg | — | events | — |
| `exec_request_quarantine` | stale exec request evidence preserved under the explicit quarantine | observation | reason, event_dir, sources, vanished | — | events | — |
| `exec_cancelled` | execute_code cancelled | anomaly | — | — | events | ✓ |
| `exec_timeout` | execute_code timed out | anomaly | — | — | events | ✓ |
| `exec_node_timeout` | node-level timeout | anomaly | — | — | events | ✓ |
| `exec_subprocess_killed` | exec child survived the signal grace period and was SIGKILLed | anomaly | pid, grace | — | events | ✓ |
| `exec_memory_guard_killed` | the host memory guard killed the largest exec process domain at critical system memory pressure | anomaly | agent_id, pid, footprint_bytes, running, pressure | — | events | — |
| `code` | LLM generated code block | noise | body, ok, duration_seconds | — | events | ✓ |
| `text` | LLM text output | noise | — | — | events | — |
| `syntax_fix` | syntax repair executed | noise | fixes | — | events | ✓ |
| `inspect_metrics_coverage_gap` | inspector statistics read shows a live recorder gap (turns without durations inside the collection era); re-emitted on every read while it holds | anomaly | family, condition, availability | — | events | — |
| `frontend_interaction` | tracked frontend interaction (click / page view / settings change) | noise | page, element, session_id, key, value | — | events | — |
| `sse_drop` | SSE event dropped | anomaly | kind, n | — | events | ✓ |
| `gateway_latency` | gateway endpoint latency — 60s aggregate per route (p50/p95/p99/max/count) | noise | route, p50_ms, p95_ms, p99_ms, max_ms, count | — | events | ✓ |
| `sse` | gateway SSE lifecycle — active connections by mode plus open/close counters | noise | mode, active_connections, opened, closed | — | events | — |
| `gateway_process` | gateway process CPU, resident memory, and open file descriptors (60s sample) | noise | cpu_percent, rss_bytes, fd_count | — | events | — |
| `gateway_event_loop` | gateway event-loop maximum callback lag and slow ticks (60s window) | noise | lag_ms, slow_ticks | — | events | — |
| `auth401_rejected` | gateway auth-401 rejections in the 60s window (aggregate count) | noise | count | — | events | — |
| `agent_registry` | agent registry max id — the agents-table high-water mark (absolute state, 60s sample) | noise | max_id | — | events | — |
| `memory_search_stats` | memory search store rows + last save duration (absolute state, 60s sample) | noise | rows, last_save_seconds | — | events | — |
| `upload_delivery_held` | a delivered upload batch was held after a permanent copy failure or its last copy attempt; nothing retries it and the person has to upload again | anomaly | agent_id, batch_id, reason | — | events | — |
| `gate_auth_probe_failed` | gate auth probe failed — carries the classification (auth/timeout/network/application) and exception shape | anomaly | category, exception_type, exception_value, status, latency_ms | — | events | — |
| `chrome_page_ttl_renewed` | Chrome page TTL deadline renewed via the renew_page tool; attributes carry page_id, ttl_s, new_expires_at | observation | — | — | events | — |
| `native_compact_proof_gap` | the actual serialized host could not establish compact source or uncertainty closure; pending intent remains held, carrying agent_id and bounded error_type without payload | anomaly | — | — | events | — |
| `impersonation_event_log_incomplete` | an impersonation event log cannot complete on its own (ended lease with an open source, or a failed capture source); re-emitted every reaper pass while it holds | anomaly | condition, lease_id, lease_machine, pending_reason, session | — | events | — |
| `host_stale_running_settled` | hosted boot settle restored rows a previous host instance left running without a task (crash / kill -9); carries n = rows settled | noise | — | — | events | — |
| `host_dispatcher_subscribed` | hosted dispatcher subscribed to the inbound wake pattern | noise | — | — | events | — |
| `host_dispatcher_reconnect` | hosted dispatcher's wake subscription dropped — reconnecting (wakes published while down are not replayed; the periodic pending scan finds them) | noise | — | — | events | — |
| `host_dispatcher_reconnect_exhausted` | hosted dispatcher's wake subscription failed repeatedly without completing a read — exiting for supervisor recovery; carries the consecutive failure count and the last exception type | anomaly | — | — | events | — |
| `host_dispatcher_scan_failed` | hosted dispatcher's durable pending scan failed; the wake subscription remains open and attributes carry the next scan backoff_s | anomaly | backoff_s | — | events | — |
| `host_dispatcher_restart_required` | a hosted turn refused its bounded cancellation after a stall — the dispatcher is exiting for supervisor recovery | anomaly | — | — | events | — |
| `host_dispatcher_bad_channel` | hosted dispatcher ignored a wake whose channel name carried no agent id | anomaly | — | — | events | — |
| `host_config_rejected` | a hosted wake was consumed without a turn because the agent's stored model config cannot build (unknown model or missing provider key) — logged once per stored config state (fingerprint); the pending inbound is kept until the overlay is fixed | anomaly | — | — | events | — |
| `host_config_normalized` | a hosted wake bound a stored llm_model pin as its registered fallback because the registry has withdrawn the pinned model — the turn and its usage attribution run on the fallback; logged once per stored config state (fingerprint) | anomaly | — | — | events | — |
| `spawn_config_normalized` | a spawn request's config_overlay carried a withdrawn llm_model — the gateway settled it to the registered fallback before the row was created, and the response carries the receipt (requested, resolved) | anomaly | — | — | events | — |
| `spawn_overlay_model_normalized` | the spawn row INSERT settled a withdrawn llm_model in the overlay to its registered fallback — the last-mile guard for client paths that compose the map outside the gateway preflight | anomaly | — | — | events | — |
| `restart_config_normalized` | a restart config_overlay carried a withdrawn llm_model — it was settled to the registered fallback before the overlay update and the restart payload | anomaly | — | — | events | — |
| `host_turn_crashed` | a hosted turn task raised — the task is dropped and the next wake retries from the checkpoint; neighbours are unaffected. Carries exception_type, plus config_fingerprint when the stored config was read before the failure | anomaly | — | — | events | — |
| `host_agent_prepared` | the host built an agent's per-agent runtime (chat model + startup reconcile) on a cold path — carries duration_ms and a reason of cold / config_changed / evicted, so a wake that pays the cold cost is distinguishable from one that does not, and a cache thrashing on config churn is visible as reason mix | noise | — | — | events | — |
| `host_started` | the hosted agent-runner finished process-scope boot and its dispatcher is live | noise | — | — | events | — |
| `host_stdout_log_rotated` | the hosted daemon rotated its raw stdout transcript at the size ceiling (task #2356) — carries size and ceiling; a crash storm shows up as repeated rotation events instead of an unbounded file | noise | — | — | events | — |
| `host_turn_uncancellable` | a hosted turn did not unwind after being cancelled — it is blocked where asyncio cannot interrupt it (a C call), so the host stopped waiting and exited. Carries the agent, how long the cancel was pending (waited_s), and the agent's real activity clock (last_active_at / idle_s from agents_meta, NOT the /api/agents field of the same name, which is MAX(inbound_messages.created_at) and goes stale during long turns — issue #183) so a slow shutdown is distinguishable from a genuine wedge. The turn resumes from its checkpoint on restart. Process mode had no equivalent because SIGKILL always lands | anomaly | — | — | events | — |
| `host_turn_stall_timeout` | the hosted stall guard aborted a graph.ainvoke whose turn clock (base/agents/observation/turn_progress.py: node enters + completed LLM steps) was silent past AVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS (turn activity = node enter, completed LLM step, streamed chunk) — the turn-level injection guard of task #2417. The invocation was cancelled and unwound; the row settles to idling; the next wake resumes from the checkpoint | anomaly | — | — | events | — |
| `host_turn_stall_uncancellable` | a stalled invocation that had been cancelled for the bounded unwind window REFUSED to unwind (blocked where asyncio cannot interrupt it — a C call). The host cannot fix this in-process: it signals a daemon restart so the supervisor recovers the turn from its checkpoint | anomaly | — | — | events | — |
| `host_turn_corpse_marked` | a hosted turn crashed and the row was stamped with the corpse marker (last_turn_fatal_at) — the reaper terminates it once the grace window elapses unless a completed turn clears the mark first | anomaly | — | — | events | — |
| `corpse_stamp_failed` | the corpse marker stamp failed after a hosted turn crash — the row keeps looking alive until a later stamp or a completed turn; the reaper cannot see this death | anomaly | — | — | events | — |
| `corpse_reaper_terminated` | the corpse reaper terminated crash-marked idling rows past the grace window (termination_source='reaper') | anomaly | — | — | events | — |
| `corpse_reaper_failed` | the beat's corpse reap pass failed — retried on the next beat; leases of healthy rows are unaffected (renewal runs first) | anomaly | — | — | events | — |
| `corpse_reaper_publish_failed` | a reaped corpse's frontend snapshot publish failed — best-effort; the durable terminated flip already committed | noise | — | — | events | — |
| `host_turn_stall_aborted` | a hosted turn task ended after its no-progress abort: the invocation unwound and was dropped; the runtime was discarded by run_turn, so the next wake re-runs the startup reconcile before resuming from the checkpoint | anomaly | — | — | events | — |
| `host_abort_reconcile_skipped` | the settled hosted turn abort skipped the immediate inbound reconcile (fail-closed) — the claimed rows are left to the next cold admission. Carries the reason: the turn's resources never fully settled (resources_unsettled), or the runtime ownership was already replaced (ownership_lost — the replacement disposes the rows) | noise | — | — | events | — |
| `host_abort_reconcile_failed` | the immediate inbound reconcile at a settled hosted turn abort raised — the host does not treat it as fatal and the next cold admission retries the disposal of the claimed rows | anomaly | — | — | events | — |
| `host_turn_reconcile_skipped` | the finished hosted turn skipped the immediate inbound reconcile (fail-closed) — the claimed rows are left to the next cold admission. Carries the reason: the turn's resources never fully settled (resources_unsettled), or the runtime ownership was already replaced (ownership_lost — the replacement disposes the rows) | noise | — | — | events | — |
| `host_turn_reconcile_failed` | the immediate inbound reconcile at a finished hosted turn raised — the host does not treat it as fatal and the next cold admission retries the disposal of the claimed rows | anomaly | — | — | events | — |
| `impersonation_aborted` | the native impersonation supervisor detected a dead core component (the executor's recorded process chain all dead/reused, or the bound relay's heartbeat stale past the exception window) and closed the lease: carries the agent, lease, session, the dead component (executor | relay) and its detail; the end note is delivered through the resume chain | anomaly | — | — | events | — |
| `impersonation_terminal_notice_failed` | the host's single submission of an ended impersonation lease's notice to its Codex thread was not accepted; carries the agent, the lease and the failure reason. Nothing retries it: the external controller learns the lease ended only on its next relay call | anomaly | lease_id, error | — | events | — |
| `host_admission_wait_exceeded` | a hosted turn has queued at the host admission gate (AVA_HOST_MAX_CONCURRENT_TURNS) for at least AVA_HOST_ADMISSION_WAIT_ALERT_SECONDS — carries the agent, its current wait, the limit and the queue depth; reported once per wait episode. Queueing is the configured memory/runtime trade-off working, not an error; a wait this long means the queue is backing up (raise the limit or inspect the turns holding slots). The wait is exempt from stall cancellation — cancelling it would only re-queue it at the tail | anomaly | — | — | events | — |
| `inbound_reconcile` | claimed inbounds consumed after interruption | noise | — | — | events | — |
| `pause_lifecycle_wait` | preparation bounded-waited in-flight work it did not author | anomaly | waited_s, outcome, agents | — | events | — |
| `pause_orphan_claim_settled` | preparation consumed (marked done, never redelivered) an ordinary claim whose runtime is gone | anomaly | agent, message_id, age_s | — | events | — |
| `host_turn_force_terminated` | this hosted turn ended on its own incarnation's applied force terminate (e.g. the delivery watchdog's hosted-turn wedge recovery): the terminate command was applied but not yet observed, the turn's fail-closed guard read refused, and the pump's own boundary observes the command; not a failure | observation | — | — | events | — |
| `host_held_wake_force_terminated` | a held-controls wake stopped quietly because its incarnation's applied force terminate landed — the pump's boundary owns the command's observation, so the wake had nothing left to do; not a failure | observation | — | — | events | — |
| `im_push_failed` | an IM bridge outbound send failed after its single retry (failures = the adapter's consecutive-failure count) | anomaly | channel, failures | — | events | — |
| `im_feishu_owner_seed_failed` | the feishu owner chat could not be restored at bridge boot; feishu notifications are blind until the user messages the bot | anomaly | reason, chats | — | events | — |
| `native_cancel_uncertain` | native cancel recovery lacks exact execution or certified stop proof; the supported consumer holds new work | anomaly | agent_id, work_id | — | events | — |
| `agent_continuation_lost` | an agent's turn failed while its continuation was owed to a draining maintenance hold; its interrupted work may be lost | anomaly | failure_category, holder, reason | — | events | — |
| `node_enter` | LangGraph node entered — sink-filtered out of the event stream (PR #1758); log files only | noise | — | — | file | — |
| `node_exit` | LangGraph node exited | noise | count, nodes | — | events | — |
| `service_started` | gateway/daemon started | noise | name, pid | — | events | ✓ |
| `halt` | turn stopped (idle/compact/system) | noise | body | — | events | ✓ |
| `agent_spawned` | agent process started | observation | spawner, forked_from | — | events | ✓ |
| `agent_resurrected` | agent resurrected | observation | — | — | events | ✓ |
| `auto_resurrect_refused` | an automatic resurrect of a terminated agent was refused (e.g. runtime_cutover_required); the triggering inbound stays queued until an operator resolves the named reason | anomaly | — | — | events | — |
| `auto_resurrect_failed` | an automatic resurrect failed for a possibly transient reason; the triggering inbound stays queued for a later or manual resurrect | observation | — | — | events | — |
| `wake_degraded` | RedisInboundListener wake path degraded (instant pub/sub wake off) | anomaly | — | — | events | — |
| `wake_restored` | RedisInboundListener wake path recovered (clean consume restored instant wake) | noise | — | — | events | — |
| `llm_usage` | LLM call metering | observation | model, calls, in_total, out_total, cache_read, cache_write_5m, cache_write_1h, reasoning, latency_ms, decode_ms, cost_usd, price_miss, price_hit, price_out, price_write_5m, price_write_1h, unpriced, usage_kind, source | — | events | ✓ |
| `turn_end` | one turn finished | observation | ok, duration_seconds | — | events | ✓ |
| `llm_turn_aborted` | turn aborted after retries | anomaly | — | LLM_ERROR | events | ✓ |
| `recovery_breaker_halt` | recovery circuit breaker tripped — consecutive permanent provider rejections halted every automatic recovery path until a turn succeeds (task #3617) | anomaly | — | — | events | — |
| `compact_turn_aborted` | turn aborted because compaction failed | anomaly | — | — | events | — |
| `llm_provider_error` | LLM provider failure | anomaly | error_class, provider, status, error_type, fatal, billing, vendor, model | LLM_ERROR | events | ✓ |
| `thinking_block_sanitized` | thinking block sanitized | noise | — | — | events | — |
| `web_fetch_injection_check_failed` | the decision-model injection check of a fetched page failed (request error, bad response or missing key) — the page is returned without a verdict. Carries model and error_type | anomaly | — | — | events | — |
| `llm_cancelled` | LLM call cancelled | anomaly | — | — | events | — |
| `compaction_completed` | applied context compaction size reduction and completed count | noise | compact_kind, compactions, history_chars, summary_chars, summary_history_ratio | — | events | — |
| `compact_request` | compact requested | noise | — | — | events | — |
| `auto_compact` | auto-compact | noise | — | — | events | ✓ |
| `compact_reminder` | compact reminder | noise | — | — | events | — |
| `circuit_breaker_open` | heartbeat circuit breaker opened | noise | — | — | events | — |
| `circuit_breaker_closed` | heartbeat circuit breaker closed | noise | — | — | events | — |
| `heartbeat_circuit_open` | heartbeat consumed while the breaker is open | noise | — | — | events | — |
| `compact_boundary_stamp` | compact boundary stamp failed (segment anchor not recorded) | noise | — | — | events | — |
| `silent_idle` | silent idle cost-boundary verdict | noise | output_tokens, cumulative_output_tokens, estimated_cost_usd, halted | — | events | — |
| `llm_retry` | LLM retry sequence completion | observation | outcome, duration_seconds | — | events | — |
| `reply_suggestion_gate` | reply suggestion gate probability | noise | — | — | events | — |
| `reply_suggestion_gate_failed` | reply suggestion gate call failed | anomaly | — | — | events | — |
| `reply_suggestion_draft_failed` | reply suggestion draft call failed | anomaly | — | — | events | — |
| `reply_suggestion_draft_rejected` | reply suggestion draft empty or too long | noise | — | — | events | — |
| `ci_usage_daily` | daily CI-minute reconciliation totals (C9) | observation | day, window_start, window_end, runs, attributed_runs, unattributed_runs, total_minutes, attributed_minutes, linux_minutes, macos_minutes, appended_runs, est_usd | — | events | — |
| `debt_sweep_daily` | daily tech-debt mechanical scan and clearing-worker dispatch | observation | day, scan_status, action, worker_agent_id | — | events | — |
| `pr_flow_daily` | daily PR-flow aggregates — ready->merged percentiles and flake discoveries (absolute gauges, one sample per complete day) | observation | day, merged_count, ready_to_merge_median_seconds, ready_to_merge_p90_seconds, flake_new_quarantines | — | events | — |
| `pr_flow_run` | PR-flow sampler run — point-in-time Trunk queue depth (absolute state) | observation | queue_depth | — | events | — |
| `ci_runs_daily` | daily CI-run aggregates | observation | repo, day, runs, instant_skip_runs, watchdog_runs, proof_runs, cancelled_runs, superseded_runs, superseded_zero_runs, failed_runs, retried_failed_runs, self_healed_runs, abandoned_runs, zombie_runs, prs_completed, prs_with_runs, per_pr_duration_median_minutes, per_pr_duration_p90_minutes, per_pr_runs_median, per_pr_runs_p90, per_pr_runs_executed_median, white_run_share, retry_share, noise_run_share, first_pass_pr_share | — | events | — |
| `ci_workflow_window` | trailing workflow fragility | observation | repo, workflow, runs, failed_runs, self_healed_runs, retried_failed_runs, cancelled_runs, superseded_runs, instant_skip_runs, prs_appeared_on, pr_appearance_share, retry_share, exec_median_seconds, exec_p90_seconds | — | events | — |
| `ci_runs_run` | CI-run sampler breadcrumb | observation | repo, window_days, window_runs, window_prs, api_requests | — | events | — |
| `task_reminder_digest` | overdue-task owner digest | noise | owner_id, task_count, task_ids | — | events | — |
| `task_escalation` | stalled-task escalation | observation | owner_id, task_count, task_ids, leg | — | events | — |
| `label_generated` | label auto-generated | noise | — | — | events | — |
| `label_generate_failed` | label generation failed | anomaly | — | — | events | — |
| `label_generate_skipped` | label generation skipped | noise | — | — | events | — |
| `label_generate_empty` | label generation empty | noise | — | — | events | — |
| `label_generate_rejected` | label generation rejected as not a label | noise | — | — | events | — |
| `understanding_enqueue_failed` | a chunk-triggered understanding job could not be enqueued (best-effort; the next trigger covers the same stretch) | anomaly | agent_id, compact_version, error | — | events | — |
| `understanding_chunk_failed` | a queued understanding chunk failed (attempted once, never retried) and stays undescribed | anomaly | agent_id, job_id, attempts, error | — | events | — |
| `understanding_chunk_skipped` | a queued understanding chunk was closed without a node (unsupported provider path or an empty chunk) | anomaly | agent_id, job_id, reason | — | events | — |
| `understanding_chunk_gap` | a chunk was described only in part: the listed stretches (overlap leftovers, turns missing from a closing snapshot) have no node | anomaly | agent_id, job_id, gaps | — | events | — |
| `understanding_snapshot_lag` | a compaction stamped its boundary before the checkpoint held the last message (the bounded wait timed out) | anomaly | agent_id, waited_seconds | — | events | — |
| `understanding_group_failed` | an upper-level understanding grouping check failed; the level's open nodes stay ungrouped until its next check threshold of new nodes arrives | anomaly | agent_id, level, open_nodes, error | — | events | — |
| `understanding_leaf_out_of_order` | an understanding leaf landed before an already grouped one; the agent's upper levels need a manual build | anomaly | agent_id, job_id, span_end | — | events | — |
| `understanding_backlog` | one sample of the understanding chunk queue per consumer round: pending, running and the oldest pending age | observation | pending, running, oldest_pending_age_seconds | — | events | — |
| `loki_write_path_probe_failed` | Loki write-path probe failed | anomaly | consecutive_failures, reason | — | events | — |
| `loki_write_path_probe_throttled` | Loki write-path probe persistently throttled | anomaly | consecutive_throttles, reason | — | events | — |
| `event_log_drop` | event-pipeline row shed | anomaly | n, queue, last_dropped_at | — | events | ✓ |
| `telemetry_read_stale` | read-side telemetry staleness detected — heartbeat older than threshold | anomaly | source, signal, threshold_s, age_s, action, reason | — | events | — |
| `telemetry_read_recovered` | read-side telemetry heartbeat recovered | observation | source, signal, stale_duration_s | — | events | — |
| `otlp_backend_disabled` | OTLP backend disabled for this process (init failure / collector unreachable); retry scheduled | anomaly | reason, endpoint | — | events | — |
| `otlp_backend_recovered` | OTLP backend brought up after a disabled episode (periodic retry) | observation | endpoint, disabled_s | — | events | — |
| `warning_resolved` | class-level warning dismissal marker | anomaly | resolved_by, category, level, event_name, source, process, agent_id, dismissed_by, note | — | events | — |
| `error_resolved` | class-level error/critical dismissal marker | anomaly | resolved_by, category, level, event_name, source, process, agent_id, dismissed_by, note | — | events | — |
| `warning_reopened` | class-level warning dismissal reopened manually or by the burst safety valve | anomaly | category, level, event_name, source, process, agent_id, dismissed_by, note, reopened_by, triggered_by_count | — | events | — |
| `error_reopened` | class-level error/critical dismissal reopened manually or by the burst safety valve | anomaly | category, level, event_name, source, process, agent_id, dismissed_by, note, reopened_by, triggered_by_count | — | events | — |
| `resolution_status` | absolute unresolved + dismissed warning/error class counts over the daemon's fixed six-hour window | noise | unresolved_warnings, unresolved_errors, dismissed_warnings, dismissed_errors, window | — | events | — |
| `checkpoint_table_sizes` | checkpoint table physical sizes and live row counts (hourly + after each blob vacuum run) | observation | blobs_bytes, checkpoints_bytes, writes_bytes, blobs_live, checkpoints_live, writes_live | — | events | — |
| `audit_write_failed` | an audit event could not be recorded in audit_events (the record is missing; the Loki projection of the same event still went out) | anomaly | event_name, error_class, error | — | events | — |
| `telemetry_store_failed` | a batch of telemetry/log events did not land in telemetry_events (the JSONL mirror and Loki still hold them) | anomaly | rows, consecutive_failures, error_class, error | — | events | — |
| `lgtm_dashboard_render_failed` | ava-ops dashboard render failed during converge; the previous provisioning file was kept | anomaly | — | — | events | — |
| `converge_file_preserved` | converge kept a locally modified destination instead of overwriting — the current content no longer matches the recorded render; repeats every converge until resolved | anomaly | path, key, surface | — | events | — |
| `root_chain_broken` | root self-check found a managed unit no longer a live child of the root process — one alert per episode, held until intact | anomaly | — | — | events | — |
| `root_restart_failed` | root unit replacement failed at its down|up half — explicit failure state recorded; intent stays running and the health monitor has a finite automatic restart budget (task #4872) | anomaly | unit, stage, detail | — | events | — |
| `root_restart_cleared` | root unit replacement succeeded — the recorded failure state was cleared (task #4872) | noise | unit, failed_for_s | — | events | — |
| `root_unit_failure_state` | root unit sits in an explicit failure state (intent running, and restart_failed, restart attempts exhausted) — emitted every health round while it holds (task #4872) | anomaly | unit, kind, detail | — | events | — |
| `root_unit_not_revivable` | root health round observed a terminal probe verdict the unit cannot revive (unknown identity, inspection error, foreign listener) — emitted every round it is observed, WARNING | anomaly | unit, detail | — | events | — |
| `permissions_helper_unhealthy` | permissions helper failed its healthcheck (ping plus launchd job classification) — one alert per episode, held until a ping-alive round | anomaly | — | — | events | — |
| `schedule_stalled` | enabled non-completed schedule has had no live session for more than two hours | anomaly | schedule_id, status, stalled_seconds | — | events | — |
| `schedule_convergence_abandoned` | the schedule manager stopped retrying a schedule whose session survives its reap or whose live session's launch provenance is unreadable, and recorded the schedule as an error; an explicit new revision lifts the stop | anomaly | schedule_id, reason, blocked_seconds | — | events | — |
| `maintenance_hold_overdue` | this home's maintenance hold, or an unreadable hold journal, has been in place for more than the configured age (default 30 minutes) and freezes every wake on the machine; one event per hold, reporting only, release it with `ava start` | anomaly | state, holder, phase, acquired_at, held_seconds | — | events | — |
| `root_health_expected` | root health observation rounds expected, including before the first sample | noise | home_id, expected_since_timestamp_seconds | — | events | — |
| `root_diagnostic` | root diagnostic sampled non-alive (every sample while it persists, WARNING) or recovered (once, INFO); observation only, no recovery authority | anomaly | diagnostic, verdict, detail, consecutive_failures | — | events | — |
| `root_health_tick` | root completed one service health and diagnostic observation round | noise | home_id, last_tick_timestamp_seconds | — | events | — |
| `postgres_stop_escalated` | a Postgres fast shutdown did not finish within its budget and was ended by an immediate shutdown plus a SIGKILL of the leftover descendants (usually a hung archive command) | anomaly | — | — | events | — |
| `backup_dump_failed` | the scheduled daily logical dump failed; it is not attempted again before the next backup hour | anomaly | detail | — | events | — |
| `recovery_drill_failed` | scheduled logical restore drill failed | anomaly | drill, detail | — | events | — |
| `health_probe_failing` | the cluster health probe found a failing check; repeated on every unhealthy run | anomaly | check, failure_class, message | — | events | — |
| `health_probe_ran` | the cluster health probe completed a run, healthy or not (its own heartbeat) | observation | unhealthy_checks | — | events | — |
| `service_start_unready` | a non-critical service missed its readiness window at ava start | anomaly | service | — | events | — |
| `boot_retries_exhausted` | the boot-time ava start failed through its whole attempt budget and is no longer retried | anomaly | attempts, exit_code | — | events | — |
| `schedule_verify_failed` | ava schedules verify found in-store schedule scripts that fail dry-import, an undefined-name read or call-signature binding, or the sweep itself failed | anomaly | checked, red, tool_error, detail | — | events | — |
| `lifecycle_pointer_stuck` | the TTL reaper's hourly scan found agents_meta.lifecycle_command_id pointing at a lifecycle command that nothing will settle on its own: one sitting at done (an out-of-band torn write, task #3678) or an applied force-terminate still unobserved an hour after it was applied (its original host crashed or its machine was decommissioned). Every resurrect of the named agent(s) defers until an operator settles the command | anomaly | count, samples | — | events | — |

## 4. Log (bare logs, category=log)

| event_name | meaning | tier | key payload fields | destination |
|------|------|------|-----------------|------|
| `page_ttl_expired` | the TTL reaper terminalized a page row whose expires_at passed; attributes carry agent_id, name, page_id | observation | — | events |
| `page_language_lookup_failed` | the gateway could not read the page copy language from user_settings (DB failure) and fell back to the default; attributes carry exc_type, exc_message | anomaly | — | events |
| `page_proxy_502` | the gateway reverse proxy could not reach a registered page server; attributes carry trace_id, agent_id, page, host, port, exc_type, exc_message | anomaly | — | events |
| `page_proxy_504` | the gateway reverse proxy timed out dialing a registered page server; attributes carry trace_id, agent_id, page, host, port, exc_type, exc_message | anomaly | — | events |
| `shell_ttl_expired` | the TTL reaper killed a persistent shell whose declared TTL passed; attributes carry agent_id, session_id, mode | observation | — | events |
| `chrome_page_ttl_expired` | the browser-mcp TTL sweep closed a Chrome page whose hard deadline passed; attributes carry page_id, url, agent_id (None when no affinity slot still named the page) | observation | — | events |
| `log` | bare log line | noise | msg | events |

## 5. SSE roles (live channel, not persisted)

Typed Pydantic discriminators in `base/events/live/projection.py` (role is a Literal);
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
| `compact_finished` | — |
| `compact_request` | — |
| `compact_started` | — |
| `error` | — |
| `exec_output` | — |
| `exec_output_chunk` | — |
| `exec_start` | — |
| `impersonation_changed` | — |
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
| `to_user_changed` | ✓ |
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
   UI display field only and never names an event.
3. **No dynamic event_name values**: event_name must come from a static literal or an
   in-code registry constant. Dynamic dimensions (agent_id, machine, node, status)
   always go into `attributes`/payload, **never into the event_name string**.
4. **No punctuation beyond underscores**: event_name charset is `[a-z0-9_]`, no
   parentheses.
5. **One fact = one event_name, no cross-category near-duplicates**: when the same
   fact exists in both audit and telemetry, distinguish by category, do not mint a
   near-synonym name.
6. **New event_names must be declared in a `base/events/declarations/` module
   first** (this document is generated from it; registration = documented), and the
   PR description cites the registry entry.
7. **Payload tiering**: only event_names whose payload fields are **branched on** by
   downstream programs get Pydantic models; display-surface payloads stay untyped
   dicts (tiering rules in `base/telemetry/audit_events.py`).

---

## 7. Inventory method and coverage (reproducible)

1. **Registry**: the `EVENTS` in `base/events/contract.py`, merged from
   `base/events/declarations/`, is the single source of truth; this document is generated by `scripts/codegen/gen_event_registry.py` (the
   pre-commit `events-registry-fresh` hook verifies no drift).
2. **Code-literal cross-check**: `python base/events/scan_kinds.py` — scans
   production Python code (excluding tests/worktrees/node_modules) for `event=`,
   `event_type=`, SSE roles, emits three inventories, and cross-checks them
   bidirectionally against the registry (`tests/contracts/test_lint_event_kinds.py`).
3. **Coverage statement**: this document covers every registered event_name (audit +
   telemetry + log + destination=file + SSE role). Not covered: test-fixture
   self-made names like `evt`/`my_event`/`some_warning` (non-production events).

**CI wiring**: `tests/contracts/test_lint_event_kinds.py` is a three-way guard — every static
`event=` literal in code must be registered (in `EVENTS`), every registry entry must
have a producer (event=/SQL/dynamic inventories), and this document must match
the generator output (drift = red). Emit-side fail-fast additionally raises
`ValueError` for unregistered names.

**When to regenerate**: run the generator before any PR that adds/renames an
event_name or SSE role; the pre-commit hook blocks drift.
