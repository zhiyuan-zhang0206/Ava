"""Which bearer the self-evolution collector presents to the gateway's /api/events.

It resolves the credential through `gateway_auth_headers`, like every other
client: an agent process presents its host's delivered machine API token and,
lacking one, fails instead of reading the human secret from the home's `.env`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from base.config import settings
from tests.skills import load_skill_script

_START = datetime.fromisoformat("2026-08-13T00:00:00+00:00")


class _EmptyPage:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"items": [], "meta": {"has_more": False}}


class _RecordingClient:
    """An /api/events server with no rows that records each request's headers."""

    def __init__(self) -> None:
        self.headers: list[dict[str, object]] = []

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get(self, url: str, *, headers: dict[str, object], **_: object) -> _EmptyPage:
        self.headers.append(headers)
        return _EmptyPage()


@pytest.fixture
def collect_mod() -> Any:
    return load_skill_script("platform", "ava-self-evolution", "scripts", "collect.py")


def _agent_process(monkeypatch: pytest.MonkeyPatch, *, delivered: str | None) -> _RecordingClient:
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "test-secret")
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    if delivered is None:
        monkeypatch.delenv("AVA_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AVA_API_TOKEN", delivered)
    return _RecordingClient()


def _fetch(collect_mod: Any, monkeypatch: pytest.MonkeyPatch, client: _RecordingClient) -> None:
    def factory(**_: Any) -> _RecordingClient:
        return client

    monkeypatch.setattr(collect_mod.httpx, "Client", factory)
    collect_mod._fetch_events_window("audit", _START, _START + timedelta(hours=1))


def test_an_agent_process_presents_the_delivered_token(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _agent_process(monkeypatch, delivered="delivered-token")

    _fetch(collect_mod, monkeypatch, client)

    assert client.headers == [{"Authorization": "Bearer delivered-token"}]


def test_an_agent_process_without_a_token_fails_instead_of_reading_the_secret(
    collect_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _agent_process(monkeypatch, delivered=None)

    with pytest.raises(RuntimeError, match="AVA_API_TOKEN"):
        _fetch(collect_mod, monkeypatch, client)

    assert client.headers == []
