"""The restarter's durable-disable boundary is enforced inside the daemon."""

from __future__ import annotations

import pytest

from services.restarter import daemon


def test_durably_disabled_restarter_exits_before_schema_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _schema_access(_url: str) -> None:
        pytest.fail("disabled restarter touched the database")

    monkeypatch.setattr(daemon, "read_skipped", lambda: {"restarter"})
    monkeypatch.setattr("shared.migrations.assert_schema_current", _schema_access)

    daemon.main()


def test_unreadable_disabled_marker_fails_closed_before_schema_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unreadable() -> set[str]:
        raise OSError("marker unavailable")

    def _schema_access(_url: str) -> None:
        pytest.fail("restarter guessed permission to start")

    monkeypatch.setattr(daemon, "read_skipped", _unreadable)
    monkeypatch.setattr("shared.migrations.assert_schema_current", _schema_access)

    daemon.main()
