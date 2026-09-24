"""Configured default windows obey their consumers' explicit request ranges."""

import pytest
from pydantic import ValidationError

from shared.config.display import DisplaySettings

DISPLAY_RANGES = (
    ("messages_default_limit", 1, 10000),
    ("timeline_default_limit", 1, 1000),
    ("notices_open_default_limit", 1, 500),
    ("notices_resolved_default_page", 1, 100),
    ("shell_capture_default_lines", 50, 2000),
    ("timeline_history_page_base", 1, 1000),
    ("timeline_window_activation_rows", 2, 5000),
    ("timeline_window_turn_rows", 1, 5000),
    ("timeline_window_measure_rows", 1, 4999),
)


def _load(monkeypatch: pytest.MonkeyPatch, name: str, value: int, source: str) -> DisplaySettings:
    alias = DisplaySettings.model_fields[name].alias
    assert isinstance(alias, str)
    if source == "env":
        monkeypatch.setenv(alias, str(value))
        return DisplaySettings()
    key = alias if source == "alias" else name
    return DisplaySettings.model_validate({key: value})


@pytest.mark.parametrize(("name", "minimum", "maximum"), DISPLAY_RANGES)
@pytest.mark.parametrize("source", ("field", "alias", "env"))
def test_display_window_accepts_both_endpoints_without_clamping(
    monkeypatch: pytest.MonkeyPatch, name: str, minimum: int, maximum: int, source: str
) -> None:
    for value in (minimum, maximum):
        configured = _load(monkeypatch, name, value, source)
        assert getattr(configured, name) == value


@pytest.mark.parametrize(("name", "minimum", "maximum"), DISPLAY_RANGES)
@pytest.mark.parametrize("source", ("field", "alias", "env"))
def test_display_window_rejects_values_outside_request_range(
    monkeypatch: pytest.MonkeyPatch, name: str, minimum: int, maximum: int, source: str
) -> None:
    for value in (minimum - 1, maximum + 1):
        with pytest.raises(ValidationError):
            _load(monkeypatch, name, value, source)
