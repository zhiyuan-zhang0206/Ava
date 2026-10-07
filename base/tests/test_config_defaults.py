"""Shipped defaults: daemon and delivery knobs, SDK contract toggles, session TTL and timeline compaction; split from base/tests/test_config.py (task #4922)."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("raw", ["[1, 2.5]", "1,2.5"])
def test_delivery_watchdog_backoff_accepts_json_or_comma_list(raw: str) -> None:
    from base.config.domains.daemon.settings import DaemonSettings

    configured = DaemonSettings.model_validate(
        {"AVA_DELIVERY_WATCHDOG_DISPATCH_BACKOFF_STEPS_S": raw}
    )
    assert configured.delivery_watchdog_dispatch_backoff_steps_s == [1.0, 2.5]


@pytest.mark.parametrize("raw", ["", "[0]", "[-1, 2]"])
def test_delivery_watchdog_backoff_rejects_empty_or_nonpositive_steps(raw: str) -> None:
    import pydantic

    from base.config.domains.daemon.settings import DaemonSettings

    with pytest.raises(pydantic.ValidationError):
        DaemonSettings.model_validate({"AVA_DELIVERY_WATCHDOG_DISPATCH_BACKOFF_STEPS_S": raw})


def test_delivery_watchdog_wake_suppression_defaults() -> None:
    from base.config.domains.daemon.settings import DaemonSettings

    configured = DaemonSettings()
    assert configured.delivery_watchdog_resurrect_fail_before_suppress == 5
    assert configured.delivery_watchdog_suppress_base_seconds == 1800.0
    assert configured.delivery_watchdog_suppress_max_seconds == 86400.0


def test_delivery_outbox_defaults() -> None:
    """Task #3757: on by default; 30s/1m/5m/15m ladder, 12h budget, 15m merge
    window, 30s flush tick, 128-entry cap (each reason lives on the field)."""
    from base.config.domains.daemon.settings import DaemonSettings

    configured = DaemonSettings()
    assert configured.delivery_outbox_enabled is True
    assert configured.delivery_outbox_retry_backoff_steps_s == [30.0, 60.0, 300.0, 900.0]
    assert configured.delivery_outbox_budget_seconds == 43200.0
    assert configured.delivery_outbox_abandoned_retention_days == 30
    assert configured.delivery_outbox_dedup_window_seconds == 900.0
    assert configured.delivery_outbox_flush_interval_seconds == 30.0
    assert configured.delivery_outbox_max_entries == 128


@pytest.mark.parametrize("raw", ["[1, 2.5]", "1,2.5"])
def test_delivery_outbox_backoff_accepts_json_or_comma_list(raw: str) -> None:
    from base.config.domains.daemon.settings import DaemonSettings

    configured = DaemonSettings.model_validate({"AVA_DELIVERY_OUTBOX_RETRY_BACKOFF_STEPS_S": raw})
    assert configured.delivery_outbox_retry_backoff_steps_s == [1.0, 2.5]


def test_reduce_context_switch_defaults_ship_as_current_behavior() -> None:
    """Task #4137: the platform switch and its policy keys land with
    behavior-preserving defaults — the off-fallback, the wiring points, and the
    night-silence window are follow-ups."""
    from base.config.domains.agent.prompt import AgentPromptSettings

    assert AgentPromptSettings().reduce_context_switch is True


def test_reduce_context_switch_env_aliases() -> None:
    from base.config.domains.agent.prompt import AgentPromptSettings

    configured = AgentPromptSettings.model_validate({"AVA_REDUCE_CONTEXT_SWITCH": "false"})
    assert configured.reduce_context_switch is False


@pytest.mark.parametrize("raw", ["", "[0]", "[-1, 2]"])
def test_delivery_outbox_backoff_rejects_empty_or_nonpositive_steps(raw: str) -> None:
    import pydantic

    from base.config.domains.daemon.settings import DaemonSettings

    with pytest.raises(pydantic.ValidationError):
        DaemonSettings.model_validate({"AVA_DELIVERY_OUTBOX_RETRY_BACKOFF_STEPS_S": raw})


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_delivery_outbox_abandoned_retention_rejects_nonpositive(raw: str) -> None:
    import pydantic

    from base.config.domains.daemon.settings import DaemonSettings

    with pytest.raises(pydantic.ValidationError):
        DaemonSettings.model_validate({"AVA_DELIVERY_OUTBOX_ABANDONED_RETENTION_DAYS": raw})


def test_sdk_code_reminder_cadence_config_contract() -> None:
    """The code-category reminder cadence is a live per-agent enum whose
    default preserves the existing once-per-context-window behavior."""
    from base.config import FIELD_INFOS, get_config_metadata

    field = FIELD_INFOS["sdk_code_reminder_cadence"]
    extra = field.json_schema_extra
    assert isinstance(extra, dict)
    assert field.default == "once_per_compaction"
    assert field.alias == "AVA_SDK_CODE_REMINDER_CADENCE"
    assert extra["per_agent"] is True
    assert extra["lifecycle"] == "live"
    meta = next(m for m in get_config_metadata() if m.name == "sdk_code_reminder_cadence")
    assert meta.choices == ["once_per_compaction", "every_time"]


def test_sdk_nameerror_hint_enabled_config_contract() -> None:
    """The assumed-persistence NameError hint is enabled by default and can be
    adjusted per agent without changing the `.env` naming surface."""
    from base.config import FIELD_INFOS

    field = FIELD_INFOS["sdk_nameerror_hint_enabled"]
    extra = field.json_schema_extra
    assert isinstance(extra, dict)
    assert field.default is True
    assert field.alias == "AVA_SDK_NAMEERROR_HINT_ENABLED"
    assert extra["per_agent"] is True
    assert extra["lifecycle"] == "live"


def test_llm_model_code_default_is_deepseek_flash() -> None:
    """An unset AVA_MODEL must fall back to deepseek-flash (user rulings 2026-09-10 / 2026-09-17)."""
    from base.config.domains.lm import LmSettings

    assert LmSettings.model_fields["llm_model"].default == "deepseek-flash"


# ─── legacy inverted aliases resolve with correct semantics ───


def test_gateway_session_ttl_defaults_to_one_day() -> None:
    from base.config.domains.gateway import GatewaySettings

    assert GatewaySettings().session_ttl_seconds == 24 * 3600


def test_timeline_compact_history_config_contract() -> None:
    from base.config import field_alias_map
    from base.config.domains.gateway import GatewaySettings

    field = GatewaySettings.model_fields["timeline_compact_history"]
    extra = field.json_schema_extra

    assert GatewaySettings().timeline_compact_history == 1
    assert field_alias_map()["timeline_compact_history"] == "AVA_TIMELINE_COMPACT_HISTORY"
    assert isinstance(extra, dict)
    assert extra["restart_required"] == "gateway"
    assert extra["writable"] is True
    assert extra["scope"] == "cluster-pinned"
    assert extra["per_agent"] is False
    assert field.description == (
        "Number of compact-history segments the timeline may load backward: "
        "0 disables compact history, -1 allows all retained segments, and N "
        "allows the newest N segments."
    )
