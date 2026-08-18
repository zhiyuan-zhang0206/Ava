"""`services.healthchecks.lgtm` unit tests — readiness probes + start.sh restart.

The stack's health is "do the four backend listeners answer" — any HTTP status
proves a listener is up; only a connection-level failure means the container
(or the docker daemon) is down, and the fix is re-running the idempotent
start.sh. The check self-gates on the $AVA_HOME/lgtm-host marker so it is a
no-op on every host that does not own the compose stack.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from services.healthchecks import lgtm as hc


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
    assert cmd == ["bash", "start.sh"]
    assert cwd.parts[-2:] == ("deploy", "lgtm")


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
    monkeypatch.setattr(hc, "down_probes", lambda: ["loki"])  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    restarted: list[bool] = []
    monkeypatch.setattr(hc, "_restart_stack", lambda: restarted.append(True) or True)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType, reportUnknownMemberType]

    hc.main()
    assert restarted == [True]

    monkeypatch.setattr(hc, "_restart_stack", lambda: False)  # pyright: ignore[reportUnknownArgumentType, reportUnknownLambdaType]
    with pytest.raises(SystemExit) as exc:
        hc.main()
    assert exc.value.code == 1
