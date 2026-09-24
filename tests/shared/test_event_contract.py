"""shared/events/contract.py — R2-C event contract registry tests.

The registry is the single source of truth for event names (design-r2 §4.3):
one declaration per event; every derived view (category projection, families,
payload keys) is a pure function of it.
"""

from __future__ import annotations

import pytest

from shared.events.contract import (
    EVENTS,
    LLM_ERROR_FAMILY,
    OPS_BUCKET_S,
    OPS_GRID_ORIGIN,
    TIER_BY_EVENT,
    category_for_kind,
    family_events,
    payload_keys,
    telemetry_events,
    tier_for,
)


def test_registry_keys_match_spec_names() -> None:
    """Every dict key equals its spec's name — a copy-paste drift in the
    registry itself must fail fast."""
    for key, spec in EVENTS.items():
        assert spec.name == key, f"key {key!r} != spec.name {spec.name!r}"


def test_categories_are_valid() -> None:
    for spec in EVENTS.values():
        assert spec.category in ("audit", "telemetry", "log")
        assert spec.extra_categories <= frozenset({"audit", "telemetry", "log"})


def test_tier_registry_covers_every_registered_event() -> None:
    assert set(TIER_BY_EVENT) == set(EVENTS)
    assert set(TIER_BY_EVENT.values()) == {"business", "anomaly", "observation", "noise"}


def test_tier_for_applies_priority_rules_and_unknown_fallback() -> None:
    assert tier_for("spawn", "audit", "info") == "business"
    assert tier_for("spawn", "audit", "warning") == "anomaly"
    assert tier_for("status_change", "audit", "info") == "business"
    assert tier_for("status_change", "telemetry", "info") == "noise"
    assert tier_for("node_exit", "telemetry", "info") == "noise"
    assert tier_for("llm_usage", "telemetry", "info") == "observation"
    assert tier_for("telemetry_read_stale", "telemetry", "info") == "anomaly"
    assert tier_for("telemetry_read_recovered", "telemetry", "info") == "observation"
    assert tier_for("otlp_backend_disabled", "telemetry", "info") == "anomaly"
    assert tier_for("otlp_backend_recovered", "telemetry", "info") == "observation"
    assert tier_for("unregistered", "telemetry", "info") == "observation"


def test_tier_for_fails_fast_when_a_registered_event_lacks_a_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(TIER_BY_EVENT, "llm_usage")

    with pytest.raises(KeyError):
        tier_for("llm_usage", "telemetry", "info")


def test_dual_category_status_change() -> None:
    """status_change genuinely carries both categories: the loguru side emits
    telemetry, audit_events emits audit (registry.md §2/§3)."""
    spec = EVENTS["status_change"]
    assert spec.category == "telemetry"
    assert "audit" in spec.extra_categories


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
    station batch) + source_tree_reset (Task #1905's source-tree guard
    repair audit).
    Bump deliberately when adding a telemetry event, never to silence a
    drift."""
    from shared.telemetry import _TELEMETRY_KINDS

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
    # reconcile close path's re-serve notice) raises it to 129; watchdog_tick
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
    # hierarchy_regen_budget_tripped) raises it to 219.
    assert "restart_cas_lost" not in _TELEMETRY_KINDS
    assert len(_TELEMETRY_KINDS) == 219
    assert payload_keys("debt_sweep_daily") == (
        "day",
        "scan_status",
        "action",
        "worker_agent_id",
    )


def test_delivery_wake_suppressed_payload_names_escalation_evidence() -> None:
    assert payload_keys("delivery_wake_suppressed") == (
        "consecutive_failures",
        "suppress_seconds",
        "suppress_count",
        "reason",
    )


def test_delivery_recovery_decision_payload_names_the_verdict() -> None:
    assert payload_keys("delivery_recovery_decision") == ("inbound_id", "decision", "reason")


def test_delivery_outbox_payloads_name_the_evidence() -> None:
    assert payload_keys("delivery_outbox_flushed") == (
        "inbound_id",
        "attempts",
        "flush_attempts",
        "age_s",
        "origin_agent_id",
    )
    assert payload_keys("delivery_outbox_abandoned") == (
        "reason",
        "detail",
        "attempts",
        "flush_attempts",
        "age_s",
        "origin_agent_id",
    )


def test_gateway_observability_payloads_and_gauge_dispositions() -> None:
    """Gateway absolute state is gauged; interval counts remain counters."""
    from shared.events.contract import payload_keys
    from shared.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("sse") == ("mode", "active_connections", "opened", "closed")
    assert payload_keys("gateway_process") == ("cpu_percent", "rss_bytes", "fd_count")
    assert payload_keys("gateway_event_loop") == ("lag_ms", "slow_ticks")
    assert {
        key: _METRIC_DISPOSITION[key]
        for key in (
            ("sse", "active_connections"),
            ("gateway_process", "cpu_percent"),
            ("gateway_process", "rss_bytes"),
            ("gateway_process", "fd_count"),
        )
    } == {
        ("sse", "active_connections"): "gauge",
        ("gateway_process", "cpu_percent"): "gauge",
        ("gateway_process", "rss_bytes"): "gauge",
        ("gateway_process", "fd_count"): "gauge",
    }


def test_checkpoint_table_sizes_payload_and_metric_disposition() -> None:
    """The hourly table-size state is emitted as six absolute gauges: three
    physical sizes plus the three live row counts (live growth vs dead-tuple
    bloat are separable in the growth curve)."""
    from shared.events.contract import payload_keys
    from shared.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("checkpoint_table_sizes") == (
        "blobs_bytes",
        "checkpoints_bytes",
        "writes_bytes",
        "blobs_live",
        "checkpoints_live",
        "writes_live",
    )
    assert {
        key: _METRIC_DISPOSITION[key]
        for key in (
            ("checkpoint_table_sizes", "blobs_bytes"),
            ("checkpoint_table_sizes", "checkpoints_bytes"),
            ("checkpoint_table_sizes", "writes_bytes"),
            ("checkpoint_table_sizes", "blobs_live"),
            ("checkpoint_table_sizes", "checkpoints_live"),
            ("checkpoint_table_sizes", "writes_live"),
        )
    } == {
        ("checkpoint_table_sizes", "blobs_bytes"): "gauge",
        ("checkpoint_table_sizes", "checkpoints_bytes"): "gauge",
        ("checkpoint_table_sizes", "writes_bytes"): "gauge",
        ("checkpoint_table_sizes", "blobs_live"): "gauge",
        ("checkpoint_table_sizes", "checkpoints_live"): "gauge",
        ("checkpoint_table_sizes", "writes_live"): "gauge",
    }


def test_exec_child_boot_payload_exposes_ready_duration() -> None:
    """The exec-child readiness event owns the boot duration measurement."""
    assert payload_keys("exec_child_boot") == ("duration_ms",)


def test_compaction_completion_payload_exposes_ratio_and_frequency() -> None:
    """Completed compactions report a character ratio and an event counter."""
    assert payload_keys("compaction_completed") == (
        "compact_kind",
        "compactions",
        "history_chars",
        "summary_chars",
        "summary_history_ratio",
    )


def test_pr_flow_payloads_and_metric_dispositions() -> None:
    """The PR-flow sampler re-emits its whole trailing window every run, so
    every numeric field is per-day absolute state: an int would default to a
    Counter and accrue across re-emissions, a float to a Histogram — each
    must be dispositioned as a gauge (task #2139)."""
    from shared.events.contract import payload_keys
    from shared.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("pr_flow_daily") == (
        "day",
        "merged_count",
        "ready_to_merge_median_seconds",
        "ready_to_merge_p90_seconds",
        "qa_rounds_mean",
        "qa_rereview_share",
        "flake_new_quarantines",
    )
    assert payload_keys("pr_flow_run") == ("queue_depth",)
    for field in (
        "merged_count",
        "ready_to_merge_median_seconds",
        "ready_to_merge_p90_seconds",
        "qa_rounds_mean",
        "qa_rereview_share",
        "flake_new_quarantines",
    ):
        assert _METRIC_DISPOSITION[("pr_flow_daily", field)] == "gauge", field
    assert _METRIC_DISPOSITION[("pr_flow_run", "queue_depth")] == "gauge"


def test_agent_registry_payload_and_metric_disposition() -> None:
    """The 60s agent-registry max-id sample is absolute state, not a sum:
    declared as an int payload (the event contract), dispositioned as a
    gauge (task #2010) — an int would otherwise default to a Counter and
    accrue value on every sample."""
    from shared.events.contract import payload_keys
    from shared.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("agent_registry") == ("max_id",)
    assert _METRIC_DISPOSITION[("agent_registry", "max_id")] == "gauge"


def test_memory_search_stats_payload_and_metric_disposition() -> None:
    """The 60s memory-search store sample is absolute state, not a sum:
    rows is an int that would otherwise default to a Counter and accrue on
    every sample; last_save_seconds a float that would default to a
    Histogram. Both must be declared gauges (task #2088)."""
    from shared.events.contract import payload_keys
    from shared.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("memory_search_stats") == ("rows", "last_save_seconds")
    assert _METRIC_DISPOSITION[("memory_search_stats", "rows")] == "gauge"
    assert _METRIC_DISPOSITION[("memory_search_stats", "last_save_seconds")] == "gauge"


def test_watchdog_tick_payload_and_metric_disposition() -> None:
    """A completed watchdog round publishes its wall-clock timestamp as a
    gauge, so Prometheus exposes freshness rather than a meaningless sum."""
    from shared.events.contract import payload_keys
    from shared.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("watchdog_tick") == ("last_tick_timestamp_seconds",)
    assert _METRIC_DISPOSITION[("watchdog_tick", "last_tick_timestamp_seconds")] == "gauge"


def test_pitr_remote_inventory_payload_and_metric_disposition() -> None:
    """A remote inventory sample is absolute backend-scoped state, never a sum."""
    from shared.events.contract import payload_keys
    from shared.telemetry.otlp.telemetry_otlp import _METRIC_DISPOSITION

    assert payload_keys("pitr_remote_inventory") == ("backend", "object_count", "bytes")
    assert _METRIC_DISPOSITION[("pitr_remote_inventory", "object_count")] == "gauge"
    assert _METRIC_DISPOSITION[("pitr_remote_inventory", "bytes")] == "gauge"


def test_stall_wave_mitigation_event_contract() -> None:
    """Task #3884: the stall-retry event carries the provider-health payload
    (vendor/model/stage/elapsed_s) and pair terminations register as an
    anomaly-tier telemetry event of their own; task #3908 declares the pair's
    typed payload too (vendor/model/stage/timeout_s)."""
    assert payload_keys("stream_stalled_retry") == ("vendor", "model", "stage", "elapsed_s")
    assert payload_keys("stream_stall_pair_terminated") == (
        "vendor",
        "model",
        "stage",
        "timeout_s",
    )
    assert tier_for("stream_stall_pair_terminated", "telemetry", "warning") == "anomaly"


def test_recovery_drill_failed_payload() -> None:
    """A failed drill names its proof type without placing failure detail in labels."""
    from shared.events.contract import payload_keys

    assert payload_keys("recovery_drill_failed") == ("drill", "detail")


def test_category_for_kind() -> None:
    assert category_for_kind("llm_usage") == "telemetry"
    assert category_for_kind("spawn") == "audit"
    assert category_for_kind("log") == "log"
    assert category_for_kind("no_such_event") == "log"  # pre-registry fallback


def test_node_enter_is_file_destination() -> None:
    """node_enter is sink-filtered out of the event stream (PR #1758) — the
    registry carries the destination so readers know where to look."""
    assert EVENTS["node_enter"].destination == "file"


def test_llm_error_family_is_the_grafana_four() -> None:
    """The LLM error family is one declaration; the pre-registry hand copies
    drifted (the retired ops_series/ops_rollup had 3, the Grafana panel had 4)."""
    fam = family_events(LLM_ERROR_FAMILY)
    assert fam == (
        "llm_turn_aborted",
        "llm_provider_error",
        "stream_stalled_retry",
        "stream_overloaded_retry",
    )


def test_payload_keys_are_the_declared_attribute_contract() -> None:
    assert payload_keys("llm_usage") == (
        "model",
        "calls",
        "in_total",
        "out_total",
        "cache_read",
        "reasoning",
        "latency_ms",
        "decode_ms",
        "cost_usd",
        "price_miss",
        "price_hit",
        "price_out",
        "unpriced",
        "task_id",
        "usage_kind",
        "source",
        "cache_mechanism",
        "cache_scope",
    )
    assert payload_keys("sse_drop") == ("kind", "n")
    assert payload_keys("spawn") == ("machine", "fork_from", "fork_checkpoint")
    assert payload_keys("agent_spawned") == ("spawner", "forked_from")
    assert payload_keys("sdk_call") == ("fn", "duration", "sample_rate", "detail")
    assert payload_keys("node_exit") == ("count", "nodes")
    assert payload_keys("heartbeat_paused") == ("duration_s",)
    assert payload_keys("task_update") == ("status",)
    assert payload_keys("process_exit") == ("reason", "pid")
    assert payload_keys("agent_boot_failed") == ("model", "error_type", "error")
    assert payload_keys("recall_filter") == ("body", "query_hmac_sha256", "picked_paths")
    assert payload_keys("passive_recall") == ("search_ms", "filter_ms")
    assert payload_keys("hook_timing") == ("hook_ms",)
    assert payload_keys("heartbeat_nudged") == ("idle_minutes",)
    assert payload_keys("heartbeat_backoff_raised") == ("level", "interval_seconds")
    assert payload_keys("heartbeat_backoff_reset") == ("previous_level", "reason")
    assert payload_keys("delivery_stalled") == ("inbound_id", "age_s")
    assert payload_keys("telemetry_read_stale") == (
        "source",
        "signal",
        "threshold_s",
        "age_s",
        "action",
        "reason",
    )
    assert payload_keys("telemetry_read_recovered") == (
        "source",
        "signal",
        "stale_duration_s",
    )
    assert payload_keys("otlp_backend_disabled") == ("reason", "endpoint")
    assert payload_keys("otlp_backend_recovered") == ("endpoint", "disabled_s")
    assert payload_keys("log") == ("msg",)  # loguru bare-log payload
    assert payload_keys("page_serve_dir_missing") == (
        "agent_id",
        "key",
        "name",
        "serve_dir",
        "port",
    )


def test_payload_keys_unknown_event_empty() -> None:
    assert payload_keys("no_such_event") == ()


def test_grid_constants_are_single_definitions() -> None:
    assert OPS_BUCKET_S == 60
    assert OPS_GRID_ORIGIN.isoformat() == "2000-01-01T00:00:00+00:00"
    # the consumers must import from the contract, not re-declare
    from gateway.ops_series_lgtm import _GRID_ORIGIN as _LG_GRID

    assert _LG_GRID == OPS_GRID_ORIGIN


def test_registry_covers_all_whitelisted_historical_names() -> None:
    """The parenthesized historical names (exec(timeout) etc.) stay registered
    — they are migration targets with live rows."""
    for name in ("exec(timeout)", "exec(failed)", "exec(cancelled)", "exec(thread-stuck)"):
        assert name in EVENTS
        assert EVENTS[name].category == "telemetry"


def test_class_resolution_markers_are_telemetry_category() -> None:
    """Class-resolution transitions are telemetry so their state can be observed.

    The resolved pair still declares the legacy target-event keys, while the
    new class keys make Loki's immutable-state transition explicit.
    """
    for name in ("warning_resolved", "error_resolved", "warning_reopened", "error_reopened"):
        spec = EVENTS[name]
        assert spec.category == "telemetry"
        assert spec.destination == "events"

    assert payload_keys("warning_resolved") == (
        "target_event_id",
        "match",
        "resolved_by",
        "category",
        "level",
        "event_name",
        "source",
        "process",
        "agent_id",
        "dismissed_by",
        "note",
    )
    assert payload_keys("resolution_status") == (
        "unresolved_warnings",
        "unresolved_errors",
        "dismissed_warnings",
        "dismissed_errors",
        "window",
    )


def test_computer_session_events_registered_as_audit() -> None:
    """The task-session envelope events (Phase 2, task #1101) are declared —
    without a spec, telemetry.emit raises ValueError and the daemon's suppress
    used to swallow it silently (task #1136)."""
    from shared.events.contract import ComputerSessionEnd, ComputerSessionStart

    start = EVENTS["computer_session_start"]
    assert start.category == "audit"
    assert start.payload is ComputerSessionStart
    end = EVENTS["computer_session_end"]
    assert end.category == "audit"
    assert end.payload is ComputerSessionEnd
