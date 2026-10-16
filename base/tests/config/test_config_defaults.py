from __future__ import annotations

from pathlib import Path

import pytest

from base import config
from base.config.service_read import ConfigAuthority


@pytest.fixture
def authority(tmp_path: Path) -> ConfigAuthority:
    return ConfigAuthority(config.settings, config.settings, tmp_path / ".env")


def test_sdk_code_reminder_cadence_config_contract(authority: ConfigAuthority) -> None:
    from base.config import FIELD_INFOS, get_config_metadata

    field = FIELD_INFOS["sdk_code_reminder_cadence"]
    extra = field.json_schema_extra
    assert isinstance(extra, dict)
    assert field.default == "once_per_compaction"
    assert field.alias == "AVA_SDK_CODE_REMINDER_CADENCE"
    assert extra["per_agent"] is True
    assert extra["lifecycle"] == "live"
    meta = next(
        m for m in get_config_metadata(authority=authority) if m.name == "sdk_code_reminder_cadence"
    )
    assert meta.choices == ["once_per_compaction", "every_time"]


def test_sdk_nameerror_hint_enabled_config_contract() -> None:
    from base.config import FIELD_INFOS

    field = FIELD_INFOS["sdk_nameerror_hint_enabled"]
    extra = field.json_schema_extra
    assert isinstance(extra, dict)
    assert field.default is True
    assert field.alias == "AVA_SDK_NAMEERROR_HINT_ENABLED"
    assert extra["per_agent"] is True
    assert extra["lifecycle"] == "live"


def test_llm_model_code_default_is_deepseek_flash() -> None:
    from base.config.domains.lm import LmSettings

    assert LmSettings.model_fields["llm_model"].default == "deepseek-flash"


def test_understand_media_model_code_default_is_gemini_3_8_flash() -> None:
    from base.config.domains.lm import LmSettings

    assert LmSettings.model_fields["understand_media_model"].default == "gemini-3.8-flash"


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
