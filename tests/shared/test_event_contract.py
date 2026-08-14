"""shared/events/contract.py — R2-C event contract registry tests.

The registry is the single source of truth for event names (design-r2 §4.3):
one declaration per event; every derived view (category projection, families,
payload keys, retention) is a pure function of it.
"""

from __future__ import annotations

from shared.events.contract import (
    EVENTS,
    LLM_ERROR_FAMILY,
    OPS_BUCKET_S,
    OPS_GRID_ORIGIN,
    category_for_kind,
    family_events,
    payload_keys,
    retention_days,
    telemetry_events,
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


def test_dual_category_status_change() -> None:
    """status_change genuinely carries both categories: the loguru side emits
    telemetry, audit_events emits audit (registry.md §2/§3)."""
    spec = EVENTS["status_change"]
    assert spec.category == "telemetry"
    assert "audit" in spec.extra_categories


def test_category_projection_matches_telemetry_whitelist() -> None:
    """The derived `_TELEMETRY_KINDS` (telemetry.py) must equal the registry's
    telemetry projection — 89 names (82 baseline + frontend_interaction
    2026-08-09 + gateway_latency Task #1091 + exec_thread_unreapable
    Task #1058 + the three CAS-race kinds from Task #688: claim_cas_lost,
    claim_cas_lost_exit, idle_cas_lost + history_dump Task #1249). Bump
    deliberately when adding a telemetry event, never to silence a drift."""
    from shared.telemetry import _TELEMETRY_KINDS

    assert telemetry_events() == frozenset(_TELEMETRY_KINDS)
    assert len(_TELEMETRY_KINDS) == 89


def test_category_for_kind() -> None:
    assert category_for_kind("llm_usage") == "telemetry"
    assert category_for_kind("spawn") == "audit"
    assert category_for_kind("log") == "log"
    assert category_for_kind("no_such_event") == "log"  # pre-registry fallback


def test_node_enter_is_file_destination() -> None:
    """node_enter is sink-filtered out of the events table (PR #1758) — the
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
    )
    assert payload_keys("sse_drop") == ("kind", "n")
    assert payload_keys("spawn") == ("machine", "fork_from", "fork_checkpoint")
    assert payload_keys("agent_spawned") == ("spawner", "forked_from")
    assert payload_keys("node_exit") == ("node", "outcome", "duration_seconds", "exc_name")
    assert payload_keys("heartbeat_paused") == ("duration_s",)
    assert payload_keys("task_update") == ("status",)
    assert payload_keys("process_exit") == ("reason", "pid")
    assert payload_keys("recall_filter") == ("body",)
    assert payload_keys("heartbeat_nudged") == ("idle_minutes",)
    assert payload_keys("delivery_stalled") == ("inbound_id", "age_s")
    assert payload_keys("log") == ("msg",)  # loguru bare-log payload


def test_payload_keys_unknown_event_empty() -> None:
    assert payload_keys("no_such_event") == ()


def test_retention_by_category_and_override() -> None:
    assert retention_days("llm_usage") == 90  # telemetry
    assert retention_days("spawn") == 365  # audit
    assert retention_days("log") == 30  # log
    assert retention_days("no_such_event") == 30


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


def test_resolved_marker_events_are_log_category() -> None:
    """warning_resolved / error_resolved are ops-panel markers: log-category,
    events-table destination, so the unresolved-views can filter them out
    (user ruling 2026-08-09)."""
    for name in ("warning_resolved", "error_resolved"):
        spec = EVENTS[name]
        assert spec.category == "log"
        assert spec.destination == "events"


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
