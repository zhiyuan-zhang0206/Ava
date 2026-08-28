"""`services.healthchecks.lgtm` unit tests — local readiness + start.sh restart.

The watchdog repairs only the local LGTM backends, so its three readiness
probes intentionally exclude remote Tempo. Any HTTP status proves a local
listener is up; only a connection-level failure re-runs the idempotent start
script. The check self-gates on the $AVA_HOME/lgtm-host marker.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from services.healthchecks import lgtm as hc


def test_readiness_probes_exclude_remote_tempo() -> None:
    """Only locally managed backends can trigger a local lifecycle repair —
    and the default settings keep the historical loopback probe URLs exactly."""
    assert hc.readiness_probes() == (
        ("loki", "http://127.0.0.1:3100/ready"),
        ("prometheus", "http://127.0.0.1:9090/-/ready"),
        ("grafana", "http://127.0.0.1:3003/api/health"),
    )


def test_readiness_probes_follow_configured_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probes derive from the observability settings base URLs, with a
    trailing slash stripped so a configured ``http://host:port/`` cannot turn
    the probe into ``//ready`` (an HTTP 404 would be counted as alive)."""
    monkeypatch.setattr(
        hc.settings.observability, "telemetry_loki_url", "http://loki.example:3100/loki/"
    )
    monkeypatch.setattr(
        hc.settings.observability, "telemetry_prometheus_url", "http://prom.example:9090/"
    )
    monkeypatch.setattr(
        hc.settings.observability, "telemetry_grafana_url", "http://grafana.example:3003"
    )
    assert hc.readiness_probes() == (
        ("loki", "http://loki.example:3100/loki/ready"),
        ("prometheus", "http://prom.example:9090/-/ready"),
        ("grafana", "http://grafana.example:3003/api/health"),
    )


def test_endpoint_answers_any_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTTPError (e.g. 503 from a warming-up backend) still proves the
    listener answered — alive."""

    def _raise(_url, **_kw):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise urllib.error.HTTPError("http://127.0.0.1:3100/ready", 503, "starting", {}, None)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(urllib.request, "urlopen", _raise)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._endpoint_answers("http://127.0.0.1:3100/ready") is True


def test_down_probes_names_connection_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the backends whose listener never answered are reported down."""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a: object) -> None:
            return None

    def _open(url: str, **_kw: object) -> _Resp:
        if ":9090" in url:
            raise OSError("connection refused")
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _open)  # pyright: ignore[reportUnknownArgumentType]
    assert hc.down_probes() == ["prometheus"]


def test_restart_runs_start_sh_in_deploy_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_restart_stack` re-runs the idempotent start.sh in deploy/lgtm."""
    calls: list[tuple[list[str], Path]] = []

    class _Result:
        returncode = 0
        stderr = ""

    def fake_run(cmd: list[str], **kw: object) -> _Result:
        calls.append((cmd, Path(str(kw["cwd"]))))
        return _Result()

    monkeypatch.setattr(hc.subprocess, "run", fake_run)  # pyright: ignore[reportUnknownArgumentType]
    assert hc._restart_stack() is True
    assert len(calls) == 1
    cmd, cwd = calls[0]
    assert cmd == ["bash", "start.sh"]
    assert cwd.parts[-2:] == ("deploy", "lgtm")


def test_main_noop_without_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every unmarked host (dev worktree clusters included) must never probe or
    restart — the native backends belong to another home's singleton."""
    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    probed: list[bool] = []
    monkeypatch.setattr(hc, "down_probes", lambda: probed.append(True) or [])  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    hc.main()
    assert probed == []


def test_main_restarts_on_down_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down backend on the marked host triggers the start.sh re-run; a failed
    re-run exits non-zero (the watchdog's failure contract)."""
    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "down_probes", lambda: ["loki"])  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    restarted: list[bool] = []
    monkeypatch.setattr(hc, "_restart_stack", lambda: restarted.append(True) or True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    hc.main()
    assert restarted == [True]

    monkeypatch.setattr(hc, "_restart_stack", lambda: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(SystemExit) as exc:
        hc.main()
    assert exc.value.code == 1


def test_is_lgtm_host_accepts_station_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The declarative observability-station capability designates the host
    exactly like the marker — the watchdog keepalive and `ava status` gate on
    either form."""
    from shared.machine import reset_identity, set_identity

    home = tmp_path / "station"
    home.mkdir()
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)

    # No marker, no capability -> not the station.
    assert hc.is_lgtm_host() is False

    # Capability form.
    set_identity(role="observability-station")
    try:
        assert hc.is_lgtm_host() is True
    finally:
        reset_identity()
    assert hc.is_lgtm_host() is False

    # Marker form.
    (home / "lgtm-host").touch()
    assert hc.is_lgtm_host() is True


def test_restart_stack_failure_reports_stdout_and_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed start.sh re-run surfaces both streams — start.sh gate failures
    (e.g. loki -verify-config rejection) are stdout log lines, and the watchdog
    must not swallow the reason."""

    class _Result:
        returncode = 1
        stdout = "loki config verify failed\n"
        stderr = "boom\n"

    monkeypatch.setattr(hc.subprocess, "run", lambda *_a, **_kw: _Result())  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]

    assert hc._restart_stack() is False
    captured = capsys.readouterr()
    assert "loki config verify failed" in captured.err
    assert "boom" in captured.err
