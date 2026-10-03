"""`notify_im` authenticates to im_bridge's `/send` with the machine API token, never the human
secret: the gateway-side daemons accept the write generation's tokens
(`base.cluster.machine.daemon_acceptance`)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from base.cluster.authority.api import API_TOKEN_ENV
from base.config import settings
from base.telemetry import alerts


class _Response:
    status_code = 200


def _capture_posts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    sent: list[dict[str, str]] = []

    def post(_url: str, *, headers: dict[str, str], **_kwargs: Any) -> _Response:
        sent.append(headers)
        return _Response()

    monkeypatch.setattr(httpx, "post", post)
    return sent


@pytest.fixture(autouse=True)
def _im_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.alerts, "im_notify_enabled", True)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "the-human-secret")


def test_the_delivered_machine_token_is_the_bearer_not_the_human_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, "machine-token")
    sent = _capture_posts(monkeypatch)

    assert alerts.notify_im("hello") is True

    assert sent == [{"Authorization": "Bearer machine-token"}]


def test_a_runner_profile_process_without_a_token_fails_the_notify_instead_of_sending_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    monkeypatch.setattr("base.cluster.machine.launcher_context", lambda: "runner")
    sent = _capture_posts(monkeypatch)

    assert alerts.notify_im("hello") is False

    assert sent == []
