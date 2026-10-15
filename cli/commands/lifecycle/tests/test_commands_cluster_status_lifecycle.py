from __future__ import annotations

import pytest

from cli.commands.cluster import control
from cli.database.tests.command_fakes import fake_session_backends as fake_session_backends
from cli.database.tests.command_fakes import gateway_role_pinned as gateway_role_pinned
from cli.database.tests.command_fakes import hermetic_gateway_base as hermetic_gateway_base
from cli.database.tests.command_inputs import command_runtime as command_runtime


def test_cmd_status_gateway_cluster_serves_line_shows_station(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        control,
        "fetch_gateway_cluster_status",
        lambda: {
            "machine_name": "station-a",
            "serve_gateway": False,
            "serve_agent_runner": False,
            "serve_observability_station": True,
            "paused": False,
        },
    )
    from cli.commands.lifecycle.status import print_gateway_cluster_status

    print_gateway_cluster_status()
    out = capsys.readouterr().out
    assert "machine_name: station-a" in out
    assert "serves:       observability-station" in out
