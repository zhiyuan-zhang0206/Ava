"""Tests for the shared transition-window severity policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shared.transition import transition_severity


@pytest.mark.parametrize(
    ("elapsed_s", "expected"),
    [
        (0, None),
        (179.999, None),
        (180, "warning"),
        (599.999, "warning"),
        (600, "error"),
    ],
)
def test_transition_severity_uses_graded_threshold_boundaries(
    elapsed_s: float, expected: str | None
) -> None:
    started_at = datetime(2026, 8, 26, tzinfo=UTC)

    assert transition_severity(started_at, started_at + timedelta(seconds=elapsed_s)) == expected


def test_live_deploy_explains_transition_at_any_age() -> None:
    started_at = datetime(2026, 8, 26, tzinfo=UTC)

    assert (
        transition_severity(
            started_at,
            started_at + timedelta(hours=1),
            deploy_explains=True,
        )
        is None
    )


def test_naive_datetimes_are_treated_as_utc() -> None:
    started_at = datetime(2026, 8, 26)  # noqa: DTZ001 — deliberately exercises naive input
    now = datetime(2026, 8, 26, tzinfo=UTC) + timedelta(minutes=3)

    assert transition_severity(started_at, now) == "warning"


def test_alert_transition_threshold_settings_defaults_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared.config import settings

    assert settings.alerts.transition_warning_seconds == 180.0
    assert settings.alerts.transition_error_seconds == 600.0

    monkeypatch.setattr(settings.alerts, "transition_warning_seconds", 45.5)
    monkeypatch.setattr(settings.alerts, "transition_error_seconds", 90.0)
    assert settings.alerts.transition_warning_seconds == 45.5
    assert settings.alerts.transition_error_seconds == 90.0
