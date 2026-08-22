"""`services.healthchecks.lgtm` unit tests — readiness probes + start.sh restart.

The stack's health is "do the four backend listeners answer" — any HTTP status
proves a listener is up; only a connection-level failure means the container
(or the docker daemon) is down, and the fix is re-running the idempotent
start.sh. The check self-gates on the $AVA_HOME/lgtm-host marker so it is a
no-op on every host that does not own the compose stack.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
import urllib.error
from collections.abc import Iterator
from pathlib import Path

import pytest

from services.healthchecks import lgtm as hc


class _OkHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def local_http_server() -> Iterator[int]:
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_grafana_root_url_is_derived_from_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc.settings.gateway, "gateway_url", "http://100.64.0.10:8000/")
    assert hc.grafana_root_url() == "http://100.64.0.10:8000/grafana/"


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "ftp://gateway:8000",
        "http://user:password@gateway:8000",
        "http://gateway:8000/base",
        "http://gateway:8000?next=https://evil.example",
    ],
)
def test_grafana_root_url_rejects_ambiguous_gateway_urls(
    monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    monkeypatch.setattr(hc.settings.gateway, "gateway_url", bad_url)
    with pytest.raises(RuntimeError, match="AVA_GATEWAY_URL"):
        hc.grafana_root_url()


def test_endpoint_answers_any_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """An HTTPError (e.g. 503 from a warming-up backend) still proves the
    listener answered — alive."""

    def _raise(_url, **_kw):  # pyright: ignore[reportMissingParameterType, reportUnknownParameterType]
        raise urllib.error.HTTPError("http://127.0.0.1:3100/ready", 503, "starting", {}, None)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(hc._NO_PROXY_OPENER, "open", _raise)  # pyright: ignore[reportPrivateUsage, reportUnknownArgumentType]
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

    monkeypatch.setattr(hc._NO_PROXY_OPENER, "open", _open)  # pyright: ignore[reportPrivateUsage, reportUnknownArgumentType]
    assert hc.down_probes() == ["prometheus"]


def test_loopback_probe_ignores_process_proxy_environment(
    local_http_server: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    assert hc._endpoint_answers(f"http://127.0.0.1:{local_http_server}/") is True


def test_obsolete_grafana_root_cleanup_preserves_secret_env_and_tightens_mode(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# preserve this comment\n"
        "OPS_ALERTS_WEBHOOK_TOKEN=keep-me\n"
        "GRAFANA_ROOT_URL=http://localhost:3003/grafana/\n"
        "GRAFANA_PG_PASSWORD=also-keep-me\n"
    )
    env_path.chmod(0o644)
    assert hc.remove_obsolete_grafana_root(tmp_path) is True
    assert env_path.read_text() == (
        "# preserve this comment\n"
        "OPS_ALERTS_WEBHOOK_TOKEN=keep-me\n"
        "GRAFANA_PG_PASSWORD=also-keep-me\n"
    )
    assert env_path.stat().st_mode & 0o777 == 0o600
    assert hc.remove_obsolete_grafana_root(tmp_path) is False


def test_restart_runs_start_sh_in_compose_dir(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert cmd[-2:] == ["bash", "start.sh"]
    assert cwd.parts[-2:] == ("deploy", "lgtm")
    assert f"GRAFANA_ROOT_URL={hc.grafana_root_url()}" in cmd
    assert f"AVA_LGTM_PYTHON={hc.sys.executable}" in cmd


def test_main_noop_without_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every unmarked host (dev worktree clusters included) must never probe or
    restart — the compose stack is another home's singleton."""
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
    monkeypatch.setattr(hc, "machine_role", lambda: frozenset({"gateway"}))
    monkeypatch.setattr(hc, "down_probes", lambda: ["loki"])  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    restarted: list[bool] = []
    monkeypatch.setattr(hc, "_restart_stack", lambda: restarted.append(True) or True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    hc.main()
    assert restarted == [True]

    monkeypatch.setattr(hc, "_restart_stack", lambda: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(SystemExit) as exc:
        hc.main()
    assert exc.value.code == 1


def test_main_rejects_marker_on_pure_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hc, "init_gateway_process", lambda _name: None)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "is_lgtm_host", lambda: True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    monkeypatch.setattr(hc, "machine_role", lambda: frozenset({"agent-runner"}))
    probed: list[bool] = []
    monkeypatch.setattr(hc, "down_probes", lambda: probed.append(True) or [])  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    with pytest.raises(SystemExit) as exc:
        hc.main()
    assert exc.value.code == 1
    assert probed == []
