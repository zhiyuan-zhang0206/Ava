"""IM Bridge healthcheck duplicate-daemon and respawn-session guards."""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from email.message import Message
from pathlib import Path

import pytest

import shared.daemon_health
import shared.paths
from ops.spec import build_services
from services.healthchecks import im_bridge as hc
from shared.config import settings
from shared.daemon_health import DaemonProbe


@pytest.fixture(autouse=True)
def _inert_process_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "init_gateway_process", lambda *_a, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]


def _spec_session() -> str:
    specs = build_services()
    return next(spec.session for spec in specs if spec.session == "im-bridge")


def test_matching_stale_holder_is_not_respawned(
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
    monkeypatch.setattr(hc, "_restart_daemon", lambda: pytest.fail("must not respawn"))

    with caplog.at_level(logging.WARNING, logger=hc._log.name):
        hc.main()

    warning = " ".join(record.getMessage() for record in caplog.records)
    assert "holder pid=4242" in warning
    assert "stale_for=130.5" in warning
    assert seen_timeouts
    assert all(timeout == shared.daemon_health._PROBE_TIMEOUT_S for timeout in seen_timeouts)


def test_unidentifiable_holder_still_respawns(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(_url: str, **_kwargs: object) -> None:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    respawns: list[int] = []
    monkeypatch.setattr(
        hc,
        "_restart_daemon",
        lambda: (respawns.append(1), DaemonProbe.up("pid 9"))[1],
    )

    hc.main()

    assert respawns == [1]


def test_restart_respawns_the_spec_session_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """_restart_daemon respawns under the ServiceSpec.session name, not the module name."""
    calls: list[tuple[str, str, Path]] = []

    def fake_respawn_and_verify(
        session: str,
        cmd: str,
        repo: Path,
        *,
        verify: Callable[[], DaemonProbe],
        **_kw: object,
    ) -> DaemonProbe:
        calls.append((session, cmd, repo))
        return verify()

    monkeypatch.setattr(hc, "respawn_and_verify", fake_respawn_and_verify)
    monkeypatch.setattr(
        shared.daemon_health,
        "probe_daemon",
        lambda *_a, **_kw: DaemonProbe.up("stub"),  # pyright: ignore[reportUnknownArgumentType]
    )

    result = hc._restart_daemon()
    assert result.alive is True
    expected_session = _spec_session()
    assert [(session, cmd) for session, cmd, _repo in calls] == [
        (expected_session, ".venv/bin/python -m services.im_bridge.daemon")
    ]
    assert expected_session == "im-bridge"


def test_spec_session_uses_kebab_case() -> None:
    """Guard the CLI-facing session name against the underscored module name."""
    specs = build_services()
    assert any(spec.session == "im-bridge" for spec in specs)
