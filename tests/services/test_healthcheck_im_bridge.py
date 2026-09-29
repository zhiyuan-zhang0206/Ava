"""IM bridge protocol and duplicate-daemon ownership probes."""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

import shared.daemon_health
import shared.paths
from services.healthchecks import im_bridge as hc
from shared.config import settings


def test_probe_accepts_matching_stale_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    home = tmp_path / "home"
    pidfile = tmp_path / "im_bridge.pid"
    pidfile.write_text("7")
    body = json.dumps(
        {"name": "im_bridge", "home": str(home), "pid": 4242, "stale_for": 130.5}
    ).encode()
    seen_timeouts: list[float] = []

    def stale_response(url: str, *, timeout: float) -> None:
        seen_timeouts.append(timeout)
        raise urllib.error.HTTPError(url, 503, "stale", Message(), io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", stale_response)
    monkeypatch.setattr(shared.paths, "ava_home", lambda: home)
    monkeypatch.setattr(settings.services, "im_bridge_pidfile", pidfile)

    with caplog.at_level(logging.WARNING, logger=hc._log.name):
        assert hc._probe().alive

    warning = " ".join(record.getMessage() for record in caplog.records)
    assert "holder pid=4242" in warning
    assert "stale_for=130.5" in warning
    assert seen_timeouts
    assert all(timeout == shared.daemon_health._PROBE_TIMEOUT_S for timeout in seen_timeouts)


def test_probe_rejects_an_unreachable_holder(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(_url: str, **_kwargs: object) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert hc._probe().alive is False
