"""Runner database URL projection for every agent-profile launch path."""

from __future__ import annotations

import pytest

from shared.cluster import derive

_OWNER_URL = "postgresql://ava:owner-password@127.0.0.1:5433/ava"
_RUNNER_URL = "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava"


def test_projects_owner_url_to_runner_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(derive.cluster, "runner_password_from_env", lambda: "runner-password")

    assert derive.runner_db_url_projection(_OWNER_URL) == _RUNNER_URL


def test_runner_url_passes_through_without_reading_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected_password_read() -> str:
        pytest.fail("runner projection must not replace an already-runner URL")

    monkeypatch.setattr(derive.cluster, "runner_password_from_env", _unexpected_password_read)

    assert derive.runner_db_url_projection(_RUNNER_URL) == _RUNNER_URL


def test_missing_runner_password_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(derive.cluster, "runner_password_from_env", lambda: "")

    with pytest.raises(RuntimeError, match="AVA_RUNNER_DB_PASSWORD is not set"):
        derive.runner_db_url_projection(_OWNER_URL)
