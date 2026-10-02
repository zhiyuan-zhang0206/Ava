"""The telemetry whitelist is the event contract's telemetry projection."""

from __future__ import annotations

from base.events.contract import payload_keys, telemetry_events


def test_category_projection_matches_telemetry_whitelist() -> None:
    """The derived `_TELEMETRY_KINDS` (telemetry.py) must equal the registry's
    telemetry projection — 106 names (2026-08-21 PR3 removed the thread
    backend's exec_thread_stuck / exec_thread_unreapable — 81 baseline +
    frontend_interaction 2026-08-09 + gateway_latency Task #1091 + the
    three CAS-race kinds from Task #688: claim_cas_lost,
    claim_cas_lost_exit, idle_cas_lost + history_dump Task #1249 +
    plugin_activation issue #40 + the seven hosted-runner kinds
    (future/infra/agent-runner-as-server.md): host_dispatcher_subscribed,
    host_dispatcher_reconnect, host_dispatcher_scan_failed, host_dispatcher_bad_channel, host_turn_crashed,
    host_agent_prepared, host_started, host_turn_uncancellable) + the two
    labeler validity kinds from issue #178: label_generate_rejected,
    label_generate_retired + exec_subprocess_killed (issue #184, the
    SIGKILL-after-grace outcome of the exec subprocess) + task_reminder_digest
    and task_escalation (Task #915 P2) + loki_query_budget (the local
    gateway-to-Loki admission state/counters) + prom_query_budget (the local
    gateway-to-Prometheus admission state/counters) + telemetry_read_stale /
    telemetry_read_recovered / otlp_backend_disabled / otlp_backend_recovered
    (runner-observability staleness and OTLP recovery state) + exec_envelope
    (2026-08-24 runner batch R-4 — exec envelope transfer size/time cost) +
    editable_pth_repaired + editable_direct_url_repaired +
    exec_editable_install_poisoned (editable-install repair audit, including
    the pre-exec poisoned-install guard) +
    checkpoint_table_sizes (Task #1545a's post-vacuum absolute gauges) +
    agent_boot_failed (Task #1704's visible process-boot failure marker) +
    gate_auth_probe_failed (Task #1736's gate auth-probe failure
    classification event) + plugin_load_failed (2026-08-28 observability
    station batch).
    Bump deliberately when adding a telemetry event, never to silence a
    drift."""
    from base.telemetry import _TELEMETRY_KINDS

    assert telemetry_events() == frozenset(_TELEMETRY_KINDS)
    # Main's exec_envelope raised this to 107; the resolution change moves two
    # legacy markers to telemetry and adds three new resolution events;
    # checkpoint_table_sizes and Task #1572's repair audit raise it to 114;
    # agent_boot_failed raises it to 115; gate_auth_probe_failed (Task #1736)
    # raises it to 116; the heartbeat circuit breaker (Task #1928) adds the
    # five breaker/emergency-compact kinds, raising it to 124; the watchdog
    # respawn breaker (Task #1941) adds respawn_breaker_open, raising it to 125;
    # the gateway auth-401 aggregate (Task #1712) adds auth401_rejected,
    # raising it to 126; hook_timing (Task #1963's per-hook node attribution)
    # raises it to 127; the agent-registry max-id gauge (Task #2010) adds
    # agent_registry, raising it to 128; archive_fetch_degraded (Task #2004)
    # raises it to 129; deleting the hibernation chain's agent_hibernating +
    # agent_swapped_in (Task #1976 phase 2) drops it back to 127;
    # memory_search_stats (Task #2088's row-growth monitoring) raises it
    # back to 128; page_restore_notified (Task #2212 direction B — the
    # reconcile close path's re-serve notice) raises it to 129; root_health_tick
    # (P1-4's completed-round freshness gauge) raises it to 131; `llm_retry`
    # records retry duration and exec_editable_install_poisoned (the pre-exec
    # poisoned-install guard, Task #2285) bring it to 133; exec_child_boot
    # and compaction_completed (Task #5643) bring the current total to 135;
    # remote PITR inventory and scheduled recovery-proof failures raise it to
    # 137; restart_handoff_host_unhealthy (Task #2338's hosted restart
    # handoff failure marker) raises it to 138; host_stale_running_settled
    # (the hosted boot settle of rows a dead host left running) raises it to
    # 139; host_dispatcher_scan_failed (Task #2255) raises it to 140;
    # host_stdout_log_rotated (task #2356's raw-transcript size rotation)
    # raises it to 141; host_config_rejected (Task #2344's wake-time model-config
    # fail-fast) raises it to 142; wake_degraded + wake_restored (Task #2418's
    # RedisInboundListener wake-health episodes) raise it to 144;
    # host_turn_stall_detected / _timeout / _uncancellable / _aborted (task
    # #2417's turn-level fake-alive detection + stall guard) raise it to 147;
    # and the schedule_self_respawn removal takes restart_cas_lost back out,
    # which the guard below keeps asserted. Gateway SSE lifecycle, process,
    # and event-loop metrics bring the total to 150; delivery_poisoned raises
    # it to 151; loki_write_path_probe_failed raises it to 152; the watchdog
    # resurrection-failure suppressor raises it to 153; schedule_stalled
    # (Task #2492's two-hour session-silence alert) raises the current total to 154;
    # heartbeat_backoff_raised + heartbeat_backoff_reset (Task #2574's B7
    # platform-side nudge backoff) raise it to 156; ci_usage_daily (Task
    # #2579's C9 daily reconciliation) raises it to 157; the corpse reaper
    # (Task #2609's crash-dead hosted rows: host_turn_corpse_marked,
    # corpse_stamp_failed, corpse_reaper_terminated, corpse_reaper_failed,
    # corpse_reaper_publish_failed) raises it to 162; shell_ttl_renewed
    # (Task #2647's explicit shell-TTL renewal) raises it to 163;
    # exec_request_quarantine (issue #2157's stale exec-evidence preservation)
    # raises it to 164; chrome_page_ttl_renewed (task #3035's Chrome page TTL)
    # raises it to 165; compact_boundary_stamp (task #3180's failed compact
    # boundary anchor stamp) raises it to 166; the root self-check (P7 W1.2b,
    # task #3338) adds root_chain_broken + root_restart_breaker_open, raising
    # it to 168; the permissions-helper healthcheck (task #3393) adds
    # permissions_helper_unhealthy + permissions_helper_repair_failed, raising
    # it to 170; the PR-flow sampler (task #2139's pr_flow_daily + pr_flow_run
    # gauges) raises the current total to 172; host_admission_wait_exceeded
    # (task #3584's fair admission queue — one report per wait episode) raises
    # the current total to 173; host_config_normalized (task #3603's wake-time
    # normalization of a withdrawn model pin) raises the current total to 174;
    # pause_lifecycle_wait (task #3591's bounded wait for an in-flight agent
    # lifecycle command) raises the current total to 175;
    # exec_request_bounded_quarantine and hosted_boot_recovery_stalled (task
    # #3619's bounded disposition of unreadable exec evidence and its
    # consecutive-boot recovery escalation) raise the current total to 177;
    # the settled-abort inbound reconcile (task #3615:
    # host_abort_reconcile_skipped + host_abort_reconcile_failed) raises the
    # current total to 179; the recovery circuit breaker (task #3617's
    # recovery_breaker_halt — the halt after consecutive permanent provider
    # rejections) raises the current total to 180; the recrash prompt reap
    # (task #3616: host_recrash_reap_skipped) raises the current total to 181;
    # the stalled crash-marked recovery decision (task #3618's
    # delivery_recovery_decision) raises the current total to 182; the
    # deferred-delivery outbox (task #3757's delivery_outbox_flushed +
    # delivery_outbox_abandoned) raises it to 184; the converge-side
    # ava-ops dashboard render-failure guard (task #3697 S3's
    # lgtm_dashboard_render_failed) raises the current total to 185; the
    # deepseek stall-wave mitigation (task #3884's stream_stall_pair_terminated
    # — two adjacent stalls ended a call early) raises the current total to 186;
    # fleet_graph_stale (task #3925's stale-serving degradation episode)
    # raises the current total to 187; the billing batch-recovery run (task
    # #3919's billing_resurrect_run — the operator-triggered post-outage rescue)
    # raises the current total to 188; the delta read-compat reconstruction
    # (task #3897's delta_read_compat — a delta-written checkpoint materialized
    # for a plain reader) raises the current total to 189; the task-registry
    # usage-write warning (task #3944's task_usage_record_failed) raises the
    # current total to 190; the stats-dashboard stale fallback (task #3973's
    # stats_dashboard_stale — the route serving its last-good response after a
    # failed recompute) raises the current total to 191; the finished-turn
    # inbound reconcile (task #3999: host_turn_reconcile_skipped +
    # host_turn_reconcile_failed) raises the current total to 193.
    # impersonation core-component death auto-stop (task #3998:
    # impersonation_aborted) raises the current total to 194; the update
    # straggler reap (task #4016: update_straggler_reaped +
    # update_straggler_reap_settled) raises the current total to 196; the
    # corpse reaper's crash-recovery wake family (task #4039:
    # crash_recovery_wake_queued / _attempted / _deferred) raises it to 199; the
    # reap-truncation close (tasks #4164/#4156: host_turn_truncated +
    # host_held_wake_truncated — the update straggler reap ending a turn or held
    # wake quietly instead of as an unclassified crash) raises the current total
    # to 201; the closure-reopen marker (task #4165: agent_reopened — a closed
    # agent's never-auto-resurrect marker cleared by an explicit resurrect)
    # raises the current total to 202; the commanded force-terminate close
    # (task #4180: host_turn_force_terminated + host_held_wake_force_terminated —
    # an applied force terminate of the turn's own incarnation ending it quietly
    # instead of as an unclassified crash) raises the current total to 204; the
    # converge-preserve signal (task #3871's converge_file_preserved — a locally
    # modified rendered destination preserved instead of overwritten) raises the
    # current total to 205; the write-side model-settlement family (task #4306:
    # spawn_config_normalized / spawn_overlay_model_normalized /
    # restart_config_normalized) raises it to 208; the daily debt-sweep dispatch
    # (task #4015: debt_sweep_daily) raises it to 209; the CI-run observability
    # trio (task #4014: ci_runs_daily / ci_workflow_window / ci_runs_run) raises
    # it to 212; persistent Loki write-path throttling raises it to 213;
    # schema_mismatch_blocked (task #4618) raises it to 214; the hierarchy
    # trigger + guardrail quintet (task #4674: hierarchy_enqueue_failed /
    # hierarchy_regen_alert / hierarchy_regen_halt / hierarchy_regen_low_reuse /
    # hierarchy_regen_budget_tripped) raises it to 219; pause_orphan_claim_settled
    # (task #4728's parked orphan settlement) raises it to 220; the backup/PITR
    # operation custody alert (backup_operation_custody) raises it to 221.
    # The recovery wake pacing pair (task #4722: host_recovery_wake_started /
    # host_recovery_wake_released) raises it to 222. The lifecycle retires the
    # source-tree repair (its source_tree_reset audit had no emitter left) and
    # adds the auto-resurrect outcome pair (auto_resurrect_refused /
    # auto_resurrect_failed); retiring the closed-agent concept
    # (decisions/2026-09-27-terminate-has-no-closed-state.md: no agent_reopened)
    # lowers the total by one, to 221. The inbound reconcile's settled-history
    # fallback (task #4788) raises it to 222. Retiring the update straggler
    # reap (decisions/2026-09-30-remove-straggler-reap.md: the reaped/settled
    # pair and the two quiet-close events) lowers it by four, to 218. The unit
    # intent store's recorded-failure pair (task #4872: root_restart_failed —
    # an interrupted replacement's explicit failure state — and
    # root_restart_cleared) raises it to 220. Retiring the PITR stack
    # (decisions/2026-10-02-delete-the-self-written-pitr-stack.md: no remote
    # inventory snapshot) lowers the current total by one, to 219. The root unit
    # alert pair (task #4872 B route: root_unit_alert_fired — a unit entered an
    # alertable failure state — and root_unit_alert_resolved) raises the
    # current total to 221. The custody reconcile audit (task #4872 C route:
    # custody_reconcile — releases always report; a retained record on first
    # sight and on evidence change, with its evidence) raises the current total
    # to 222. The Postgres stop escalation (postgres_stop_escalated — a fast
    # shutdown ended by an immediate one, with the leftover processes killed)
    # raises it to 223.
    assert "restart_cas_lost" not in _TELEMETRY_KINDS
    assert "agent_reopened" not in _TELEMETRY_KINDS
    for retired in (
        "update_straggler_reaped",
        "update_straggler_reap_settled",
        "host_turn_truncated",
        "host_held_wake_truncated",
    ):
        assert retired not in _TELEMETRY_KINDS
    # The suffix diagnostic adds one; retiring tool-call concatenation removes one.
    assert "multiple_tool_calls_merged" not in _TELEMETRY_KINDS
    assert len(_TELEMETRY_KINDS) == 223
    assert payload_keys("debt_sweep_daily") == (
        "day",
        "scan_status",
        "action",
        "worker_agent_id",
    )
