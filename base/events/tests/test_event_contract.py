"""base/events/contract.py — R2-C event contract registry tests.

The registry is the single source of truth for event names (design-r2 §4.3):
one declaration per event; every derived view (category projection, families,
payload keys) is a pure function of it.
"""

from __future__ import annotations

import pytest

from base.events.contract import (
    EVENTS,
    LLM_ERROR_FAMILY,
    TIER_BY_EVENT,
    category_for_kind,
    family_events,
    payload_keys,
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


def test_root_restart_payloads_name_the_evidence() -> None:
    from base.events.contract import payload_keys

    assert payload_keys("root_restart_failed") == ("unit", "stage", "detail")
    assert payload_keys("root_restart_cleared") == ("unit", "failed_for_s")


def test_custody_reconcile_payload_names_the_evidence() -> None:
    from base.events.contract import payload_keys

    assert payload_keys("custody_reconcile") == (
        "unit",
        "checked",
        "found",
        "decision",
        "evidence",
    )


def test_root_unit_alert_payloads_name_the_evidence() -> None:
    from base.events.contract import payload_keys

    assert payload_keys("root_unit_alert_fired") == (
        "unit",
        "kind",
        "since_timestamp_seconds",
        "detail",
        "delivery",
    )
    assert payload_keys("root_unit_alert_resolved") == (
        "unit",
        "kind",
        "since_timestamp_seconds",
        "failed_for_s",
        "delivery",
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


def test_backup_operation_custody_payload() -> None:
    """Custody alerts group by operation kind; the detail is never a label."""
    from base.events.contract import payload_keys

    assert payload_keys("backup_operation_custody") == ("operation", "custody", "detail")


def test_recovery_drill_failed_payload() -> None:
    """A failed drill names its proof type without placing failure detail in labels."""
    from base.events.contract import payload_keys

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


_DECLARED_PAYLOAD_KEYS = {
    "llm_usage": (
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
    ),
    "sse_drop": ("kind", "n"),
    "spawn": ("machine", "fork_from", "fork_checkpoint"),
    "agent_spawned": ("spawner", "forked_from"),
    "sdk_call": ("fn", "duration", "sample_rate", "detail"),
    "node_exit": ("count", "nodes"),
    "heartbeat_paused": ("duration_s",),
    "task_update": ("status",),
    "process_exit": ("reason", "pid"),
    "agent_boot_failed": ("model", "error_type", "error"),
    "recall_filter": ("body", "query_hmac_sha256", "picked_paths"),
    "passive_recall": ("search_ms", "filter_ms"),
    "hook_timing": ("hook_ms",),
    "heartbeat_nudged": ("idle_minutes",),
    "heartbeat_backoff_raised": ("level", "interval_seconds"),
    "heartbeat_backoff_reset": ("previous_level", "reason"),
    "delivery_stalled": ("inbound_id", "age_s"),
    "telemetry_read_stale": (
        "source",
        "signal",
        "threshold_s",
        "age_s",
        "action",
        "reason",
    ),
    "telemetry_read_recovered": (
        "source",
        "signal",
        "stale_duration_s",
    ),
    "otlp_backend_disabled": ("reason", "endpoint"),
    "otlp_backend_recovered": ("endpoint", "disabled_s"),
    "log": ("msg",),  # loguru bare-log payload
    "page_serve_dir_missing": (
        "agent_id",
        "key",
        "name",
        "serve_dir",
        "port",
    ),
}


def test_payload_keys_are_the_declared_attribute_contract() -> None:
    for event, keys in _DECLARED_PAYLOAD_KEYS.items():
        assert payload_keys(event) == keys, event


def test_payload_keys_unknown_event_empty() -> None:
    assert payload_keys("no_such_event") == ()


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
    from base.events.declarations.audit import ComputerSessionEnd, ComputerSessionStart

    start = EVENTS["computer_session_start"]
    assert start.category == "audit"
    assert start.payload is ComputerSessionStart
    end = EVENTS["computer_session_end"]
    assert end.category == "audit"
    assert end.payload is ComputerSessionEnd
