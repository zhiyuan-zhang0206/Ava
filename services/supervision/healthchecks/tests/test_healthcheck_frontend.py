"""Frontend availability probes the application behind the entry gate."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.supervision.healthchecks import frontend as hc


@pytest.mark.parametrize("returncode,ready", [(0, True), (7, False)])
def test_application_http_result_controls_availability(
    monkeypatch: pytest.MonkeyPatch, returncode: int, ready: bool
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], returncode))
    monkeypatch.setattr(hc.subprocess, "run", run)
    monkeypatch.setattr(
        hc,
        "settings",
        SimpleNamespace(
            services=SimpleNamespace(
                frontend_healthcheck_url="http://localhost:3000",
                app_port=3001,
            )
        ),
    )
    assert hc.probe_frontend().alive is ready
    assert run.call_args.args[0][-1] == "http://localhost:3001"
    assert run.call_args.kwargs["timeout"] == 5


@pytest.mark.parametrize("error", [FileNotFoundError("curl"), subprocess.TimeoutExpired("curl", 5)])
def test_missing_or_timed_out_http_probe_is_down(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(hc.subprocess, "run", Mock(side_effect=error))
    assert not hc.probe_frontend().alive


def test_healthy_http_needs_no_root_or_socket_census(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("frontend availability is an HTTP observation")

    monkeypatch.setattr("psutil.process_iter", forbidden)
    monkeypatch.setattr("base.native_process.root_control.client.owned_process", forbidden)
    monkeypatch.setattr(hc.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0)))
    assert hc.probe_frontend().alive
