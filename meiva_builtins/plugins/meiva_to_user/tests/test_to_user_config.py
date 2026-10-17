from __future__ import annotations

from base.config import FIELD_INFOS, get_config_metadata
from base.config.service_read import ConfigAuthority


def test_the_soft_limit_ships_at_1500_words_per_agent() -> None:
    field = FIELD_INFOS["to_user_soft_limit_words"]
    extra = field.json_schema_extra
    assert field.default == 1500
    assert field.alias == "MEIVA_TO_USER_SOFT_LIMIT_WORDS"
    assert isinstance(extra, dict)
    assert extra["per_agent"] is True
    assert extra["lifecycle"] == "live"


def test_the_reminder_cadence_ships_once_per_compaction(
    config_authority: ConfigAuthority,
) -> None:
    field = FIELD_INFOS["to_user_limit_reminder_cadence"]
    extra = field.json_schema_extra
    assert field.default == "once_per_compaction"
    assert field.alias == "MEIVA_TO_USER_LIMIT_REMINDER_CADENCE"
    assert isinstance(extra, dict)
    assert extra["per_agent"] is True
    meta = next(
        m
        for m in get_config_metadata(authority=config_authority)
        if m.name == "to_user_limit_reminder_cadence"
    )
    assert meta.choices == ["once_per_compaction", "every_time"]
