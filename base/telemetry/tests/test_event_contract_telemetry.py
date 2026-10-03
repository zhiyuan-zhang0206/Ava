"""The telemetry whitelist is the event contract's telemetry projection."""

from __future__ import annotations

from base.events.contract import payload_keys, telemetry_events


def test_category_projection_matches_telemetry_whitelist() -> None:
    """The derived `_TELEMETRY_KINDS` (telemetry.py) must equal the registry's
    telemetry projection; the registry is the only place a name is counted."""
    from base.telemetry import _TELEMETRY_KINDS

    assert telemetry_events() == frozenset(_TELEMETRY_KINDS)
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
    assert payload_keys("debt_sweep_daily") == (
        "day",
        "scan_status",
        "action",
        "worker_agent_id",
    )
