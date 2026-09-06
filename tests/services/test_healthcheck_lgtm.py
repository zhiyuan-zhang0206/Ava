"""`services.healthchecks.lgtm` unit tests — readiness and Loki write-path repair.

The watchdog repairs only the local LGTM backends, so its three readiness
probes intentionally exclude remote Tempo. Any HTTP status proves a local
listener is up; only a connection-level failure re-runs the idempotent start
script immediately. Once listeners answer, three generic Loki write/read probe
failures trigger the same repair. A body-qualified stuck ingester is force-restarted
immediately when its storage disk is below the WAL throttle threshold. The check
self-gates on the $AVA_HOME/lgtm-host marker.
"""

from __future__ import annotations

import email.message
import io
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest

import shared.cluster
import shared.lgtm_systemd
import shared.proc
from services.healthchecks import lgtm as hc


@pytest.fixture(autouse=True)
def _darwin_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc.platform, "system", lambda: "Darwin")


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


def test_readiness_probes_ignore_remote_query_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remote read endpoints cannot falsely mark a local native service alive."""
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
        ("loki", "http://127.0.0.1:3100/ready"),
        ("prometheus", "http://127.0.0.1:9090/-/ready"),
        ("grafana", "http://127.0.0.1:3003/api/health"),
    )


def test_endpoint_answers_any_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTTPError (e.g. 503 from a warming-up backend) still proves the
    listener answered — alive."""

    def _raise(_url, **_kw):
        raise urllib.error.HTTPError("http://127.0.0.1:3100/ready", 503, "starting", {}, None)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(hc._local_http, "open", _raise)  # pyright: ignore[reportUnknownArgumentType]
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

    monkeypatch.setattr(hc._local_http, "open", _open)
    assert hc.down_probes() == ["prometheus"]


def test_write_path_probe_rejects_400_push(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_request: object, **_kwargs: object) -> None:
        raise urllib.error.HTTPError("http://loki/otlp/v1/logs", 400, "rejected", {}, None)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(hc._local_http, "open", _raise)

    assert hc.write_path_probe() == (False, "push_http_400")


def test_write_path_probe_identifies_stuck_ingester(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_request: object, **_kwargs: object) -> None:
        raise urllib.error.HTTPError(  # pyright: ignore[reportArgumentType]
            "http://loki/otlp/v1/logs",
            500,
            "internal error",
            email.message.Message(),
            io.BytesIO(b"RPC error: code = Unknown desc = InGeStEr Is ShUtTiNg DoWn"),
        )

    monkeypatch.setattr(hc._local_http, "open", _raise)

    assert hc.write_path_probe() == (False, "ingester_shutting_down")


def test_write_path_probe_does_not_misclassify_plain_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_request: object, **_kwargs: object) -> None:
        raise urllib.error.HTTPError(  # pyright: ignore[reportArgumentType]
            "http://loki/otlp/v1/logs",
            503,
            "throttled",
            email.message.Message(),
            io.BytesIO(b"write throttled because disk usage is too high"),
        )

    monkeypatch.setattr(hc._local_http, "open", _raise)

    assert hc.write_path_probe() == (False, "push_http_503")


def test_write_path_probe_reports_push_request_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_request: object, **_kwargs: object) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(hc._local_http, "open", _raise)

    assert hc.write_path_probe() == (False, "push_error")


def test_write_path_probe_reports_marker_not_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[urllib.request.Request] = []

    def _open(request: urllib.request.Request, **_kwargs: object) -> _Response:
        requests.append(request)
        if len(requests) == 1:
            return _Response(status=204)
        return _Response(status=200, body=b'{"data":{"result":[]}}')

    monkeypatch.setattr(hc._local_http, "open", _open)

    assert hc.write_path_probe() == (False, "probe_not_visible")
    request_body = requests[0].data
    assert isinstance(request_body, bytes)
    payload = cast(dict[str, Any], json.loads(request_body))
    record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    timestamp = record["timeUnixNano"]
    marker = record["body"]["stringValue"]
    assert timestamp.isdigit()
    assert marker == f"watchdog-write-probe-{timestamp}"
    assert requests[0].full_url.endswith("/otlp/v1/logs")
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
            resource_log = payload["resourceLogs"][0]
            attributes = {
                attribute["key"]: attribute["value"]["stringValue"]
                for attribute in resource_log["resource"]["attributes"]
            }
            record = resource_log["scopeLogs"][0]["logRecords"][0]
            marker = record["body"]["stringValue"]
            assert attributes == {
                "agent_id": marker,
                "event_name": "watchdog-write-probe",
            }
            assert record["timeUnixNano"] == marker.removeprefix("watchdog-write-probe-")
            return _Response(status=204)
        body = json.dumps({"data": {"result": [{"values": [["1", marker]]}]}}).encode()
        return _Response(status=200, body=body)

    monkeypatch.setattr(hc._local_http, "open", _open)

    assert hc.write_path_probe() == (True, "ok")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(requests[1].full_url).query)
    assert query["query"] == [f'{{agent_id="{marker}"}}']
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

    monkeypatch.setattr(hc._local_http, "open", _open)

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

    monkeypatch.setattr(hc.subprocess, "run", fake_run)
    assert hc._restart_stack() is True
    assert len(calls) == 1
    cmd, cwd = calls[0]
    assert cmd == ["bash", "start.sh"]
    assert cwd.parts[-2:] == ("deploy", "lgtm")


def test_force_restart_loki_reports_exception_without_raising(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("launchctl unavailable")

    monkeypatch.setattr(shared.proc, "run_bounded", _unavailable)

    assert hc._force_restart_loki(tmp_path) is False
    assert "launchctl unavailable" in capsys.readouterr().err


def test_main_noop_without_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every unmarked host (dev worktree clusters included) must never probe or
    restart — the native backends belong to another home's singleton."""
    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: False)
    probed: list[bool] = []
    monkeypatch.setattr(hc, "down_probes", lambda: probed.append(True) or [])

    hc.main()
    assert probed == []


def test_write_counter_survives_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """A counter write failure (e.g. full disk) must not crash the round."""
    monkeypatch.setattr(hc, "_write_probe_counter_path", lambda: Path("/no-such-dir/x"))

    hc._write_counter(3)  # no raise — the lost increment only delays the verdict


def test_main_restarts_on_down_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A down backend on the marked host triggers the start.sh re-run; a failed
    re-run exits non-zero (the watchdog's failure contract)."""
    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: True)
    monkeypatch.setattr(hc, "down_probes", lambda: ["loki"])
    counters: list[int] = []
    monkeypatch.setattr(hc, "_write_counter", counters.append)
    write_probed: list[bool] = []
    monkeypatch.setattr(hc, "write_path_probe", lambda: write_probed.append(True) or (True, "ok"))
    restarted: list[bool] = []
    monkeypatch.setattr(hc, "_restart_stack", lambda: restarted.append(True) or True)

    hc.main()
    assert restarted == [True]
    assert counters == [0]
    assert write_probed == []

    monkeypatch.setattr(hc, "_restart_stack", lambda: False)
    with pytest.raises(SystemExit) as exc:
        hc.main()
    assert exc.value.code == 1


def test_main_restarts_on_third_write_probe_failure_and_emits_each_round(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: True)
    monkeypatch.setattr(hc, "down_probes", list)
    monkeypatch.setattr(hc, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(hc, "write_path_probe", lambda: (False, "probe_not_visible"))
    restarts: list[bool] = []
    monkeypatch.setattr(hc, "_restart_stack", lambda: restarts.append(True) or True)
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


def test_main_kickstarts_stuck_ingester_when_disk_is_below_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Usage:
        total = 100
        used = 94

    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: True)
    monkeypatch.setattr(hc, "down_probes", list)
    monkeypatch.setattr(hc, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(hc.settings.observability, "lgtm_storage_dir", "")
    monkeypatch.setattr(hc, "write_path_probe", lambda: (False, "ingester_shutting_down"))
    inspected: list[Path] = []

    def _disk_usage(path: object) -> _Usage:
        inspected.append(Path(str(path)))
        return _Usage()

    monkeypatch.setattr(shutil, "disk_usage", _disk_usage)
    monkeypatch.setattr(os, "getuid", lambda: 501)
    calls: list[tuple[list[str], float]] = []

    def _run_bounded(
        argv: list[str], *, timeout: float, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(shared.proc, "run_bounded", _run_bounded)
    emitted: list[dict[str, object]] = []

    def _emit(*_args: object, **kwargs: object) -> None:
        attributes = kwargs["attributes"]
        if isinstance(attributes, dict):
            emitted.append(cast(dict[str, object], attributes))

    monkeypatch.setattr(hc.telemetry, "emit", _emit)

    hc.main()

    label = f"com.ava.loki.{shared.cluster.home_slug(tmp_path)}"
    assert calls == [(["launchctl", "kickstart", "-k", f"gui/501/{label}"], 45)]
    assert inspected == [(tmp_path / "lgtm/native/data").resolve()]
    assert hc._read_counter() == 0
    assert emitted == [{"consecutive_failures": 1, "reason": "ingester_shutting_down"}]
    assert "force-restarting Loki" in capsys.readouterr().err


def test_main_does_not_kickstart_stuck_ingester_at_disk_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Usage:
        total = 100
        used = 95

    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: True)
    monkeypatch.setattr(hc, "down_probes", list)
    monkeypatch.setattr(hc, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(hc.settings.observability, "lgtm_storage_dir", str(tmp_path / "data"))
    monkeypatch.setattr(hc, "write_path_probe", lambda: (False, "ingester_shutting_down"))
    inspected: list[Path] = []

    def _disk_usage(path: object) -> _Usage:
        inspected.append(Path(str(path)))
        return _Usage()

    def _deny(*_args: object, **_kwargs: object) -> None:
        pytest.fail("high disk must suppress kickstart")

    def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(shutil, "disk_usage", _disk_usage)
    monkeypatch.setattr(shared.proc, "run_bounded", _deny)
    monkeypatch.setattr(hc.telemetry, "emit", _noop)

    hc.main()

    assert inspected == [(tmp_path / "data").resolve()]
    assert hc._read_counter() == 1


def test_main_force_restarts_stuck_ingester_through_systemd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Usage:
        total = 100
        used = 10

    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: True)
    monkeypatch.setattr(hc, "down_probes", list)
    monkeypatch.setattr(hc, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(hc.settings.observability, "lgtm_storage_dir", "")
    monkeypatch.setattr(hc, "write_path_probe", lambda: (False, "ingester_shutting_down"))

    def _disk_usage(_path: object) -> _Usage:
        return _Usage()

    monkeypatch.setattr(shutil, "disk_usage", _disk_usage)
    monkeypatch.setattr(hc.platform, "system", lambda: "Linux")
    calls: list[tuple[Path, str]] = []

    def _force_restart(home: Path, name: str) -> None:
        calls.append((home, name))

    def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(shared.lgtm_systemd, "force_restart", _force_restart)
    monkeypatch.setattr(hc.telemetry, "emit", _noop)

    hc.main()

    assert calls == [(tmp_path, "loki")]
    assert hc._read_counter() == 0


def test_main_successful_write_probe_clears_counter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: True)
    monkeypatch.setattr(hc, "down_probes", list)
    monkeypatch.setattr(hc, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(hc, "write_path_probe", lambda: (True, "ok"))
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

    monkeypatch.setattr(hc.subprocess, "run", lambda *_a, **_kw: _Result())  # pyright: ignore[reportUnknownArgumentType]

    assert hc._restart_stack() is False
    captured = capsys.readouterr()
    assert "loki config verify failed" in captured.err
    assert "boom" in captured.err
