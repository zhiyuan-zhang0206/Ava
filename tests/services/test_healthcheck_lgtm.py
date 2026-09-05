"""`services.healthchecks.lgtm` unit tests — readiness and Loki write-path repair.

The watchdog repairs only the local LGTM backends, so its three readiness
probes intentionally exclude remote Tempo. Any HTTP status proves a local
listener is up; only a connection-level failure re-runs the idempotent start
script immediately. Once listeners answer, three failed Loki write/read probes
trigger the same repair. The check self-gates on the $AVA_HOME/lgtm-host marker.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest

from services.healthchecks import lgtm as hc


class _Response:
    def __init__(self, *, status: int, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


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


def test_write_path_probe_rejects_non_2xx_push(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_request: object, **_kwargs: object) -> None:
        raise urllib.error.HTTPError("http://loki/push", 429, "throttled", {}, None)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(urllib.request, "urlopen", _raise)  # pyright: ignore[reportUnknownArgumentType]

    assert hc.write_path_probe() == (False, "push_http_429")


def test_write_path_probe_reports_push_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_request: object, **_kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)  # pyright: ignore[reportUnknownArgumentType]

    assert hc.write_path_probe() == (False, "push_error")


def test_write_path_probe_reports_marker_not_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[urllib.request.Request] = []

    def _open(request: urllib.request.Request, **_kwargs: object) -> _Response:
        requests.append(request)
        if len(requests) == 1:
            return _Response(status=204)
        return _Response(status=200, body=b'{"data":{"result":[]}}')

    monkeypatch.setattr(urllib.request, "urlopen", _open)  # pyright: ignore[reportUnknownArgumentType]

    assert hc.write_path_probe() == (False, "probe_not_visible")
    request_body = requests[0].data
    assert isinstance(request_body, bytes)
    payload = cast(dict[str, Any], json.loads(request_body))
    stream = payload["streams"][0]
    assert stream["stream"] == {"probe_id": "watchdog-write"}
    timestamp, marker = stream["values"][0]
    assert timestamp.isdigit()
    assert marker == f"watchdog-write-probe-{timestamp}"
    assert requests[0].full_url.endswith("/loki/api/v1/push")
    assert requests[0].get_header("X-scope-orgid") == "fake"
    assert requests[0].get_header("Content-type") == "application/json"


def test_write_path_probe_finds_marker_in_numeric_query_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []
    marker = ""

    def _open(request: urllib.request.Request, **_kwargs: object) -> _Response:
        nonlocal marker
        requests.append(request)
        if len(requests) == 1:
            request_body = request.data
            assert isinstance(request_body, bytes)
            payload = cast(dict[str, Any], json.loads(request_body))
            marker = payload["streams"][0]["values"][0][1]
            return _Response(status=204)
        body = json.dumps({"data": {"result": [{"values": [["1", marker]]}]}}).encode()
        return _Response(status=200, body=body)

    monkeypatch.setattr(urllib.request, "urlopen", _open)  # pyright: ignore[reportUnknownArgumentType]

    assert hc.write_path_probe() == (True, "ok")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(requests[1].full_url).query)
    assert query["query"] == ['{probe_id="watchdog-write"}']
    assert query["start"][0].isdigit()
    assert query["end"][0].isdigit()
    marker_ts = int(marker.removeprefix("watchdog-write-probe-"))
    assert int(query["end"][0]) > marker_ts  # range end is exclusive
    assert int(query["end"][0]) - int(query["start"][0]) >= 121_000_000_000
    assert "/loki/api/v1/query_range?" in requests[1].full_url


def test_write_path_probe_reports_query_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def _open(_request: object, **_kwargs: object) -> _Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Response(status=204)
        raise OSError("query unavailable")

    monkeypatch.setattr(urllib.request, "urlopen", _open)  # pyright: ignore[reportUnknownArgumentType]

    assert hc.write_path_probe() == (False, "query_error")


def test_write_probe_counter_round_trip_and_corruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(hc, "ava_home", lambda: tmp_path)

    assert hc._write_probe_counter_path() == tmp_path / "lgtm-write-probe-consecutive-failures"
    assert hc._read_counter() == 0
    hc._write_counter(2)
    assert hc._read_counter() == 2
    hc._write_probe_counter_path().write_text("not-an-int", encoding="utf-8")
    assert hc._read_counter() == 0


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


def test_write_counter_survives_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """A counter write failure (e.g. full disk) must not crash the round."""
    monkeypatch.setattr(hc, "_write_probe_counter_path", lambda: Path("/no-such-dir/x"))  # pyright: ignore[reportUnknownArgumentType]

    hc._write_counter(3)  # no raise — the lost increment only delays the verdict


def test_main_restarts_on_down_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down backend on the marked host triggers the start.sh re-run; a failed
    re-run exits non-zero (the watchdog's failure contract)."""
    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "down_probes", lambda: ["loki"])  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    counters: list[int] = []
    monkeypatch.setattr(hc, "_write_counter", counters.append)
    write_probed: list[bool] = []
    monkeypatch.setattr(hc, "write_path_probe", lambda: write_probed.append(True) or (True, "ok"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    restarted: list[bool] = []
    monkeypatch.setattr(hc, "_restart_stack", lambda: restarted.append(True) or True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    hc.main()
    assert restarted == [True]
    assert counters == [0]
    assert write_probed == []

    monkeypatch.setattr(hc, "_restart_stack", lambda: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(SystemExit) as exc:
        hc.main()
    assert exc.value.code == 1


def test_main_restarts_on_third_write_probe_failure_and_emits_each_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "down_probes", list)
    monkeypatch.setattr(hc, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(hc, "write_path_probe", lambda: (False, "probe_not_visible"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    restarts: list[bool] = []
    monkeypatch.setattr(hc, "_restart_stack", lambda: restarts.append(True) or True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _emit(*args: object, **kwargs: object) -> None:
        emitted.append((args, kwargs))

    monkeypatch.setattr(hc.telemetry, "emit", _emit)

    hc.main()
    hc.main()
    assert restarts == []
    assert hc._read_counter() == 2

    hc.main()
    assert restarts == [True]
    assert hc._read_counter() == 0
    assert [entry[1]["attributes"] for entry in emitted] == [
        {"consecutive_failures": 1, "reason": "probe_not_visible"},
        {"consecutive_failures": 2, "reason": "probe_not_visible"},
        {"consecutive_failures": 3, "reason": "probe_not_visible"},
    ]
    assert all(entry[0] == ("telemetry", "loki_write_path_probe_failed") for entry in emitted)
    assert all(entry[1]["level"] == "warning" for entry in emitted)
    assert all(entry[1]["source"] == "system" for entry in emitted)
    assert "write path probe failed 3 consecutive rounds" in capsys.readouterr().err


def test_main_successful_write_probe_clears_counter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "down_probes", list)
    monkeypatch.setattr(hc, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(hc, "write_path_probe", lambda: (True, "ok"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    hc._write_counter(2)

    hc.main()

    assert hc._read_counter() == 0


def test_is_lgtm_host_accepts_station_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The declarative observability-station capability designates the host
    exactly like the marker — the watchdog keepalive and `ava status` gate on
    either form."""
    from shared.machine import reset_identity, set_identity

    home = tmp_path / "station"
    home.mkdir()
    # is_lgtm_host() dials the home twice: the name bound in this module
    # (from shared.paths import ava_home) AND the fresh lookup inside
    # home_is_observability_station — patch both.
    monkeypatch.setattr(hc, "ava_home", lambda: home)
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
