"""`cli status` on a gateway cluster shows the observability station line."""

from __future__ import annotations

import pytest

from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base
from tests.cli._commands_helpers import _noop_start_prechecks as _noop_start_prechecks


def test_cmd_status_gateway_cluster_serves_line_shows_station(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava status`'s gateway cluster-status supplement renders the station
    capability in the serves: line when the gateway snapshot carries it
    (the function imports fetch_gateway_cluster_status at call time, so the
    module attribute patch is the rebind that takes effect)."""
    monkeypatch.setattr(
        "cli.commands.cluster.control.fetch_gateway_cluster_status",
        lambda: {
            "machine_name": "station-a",
            "serve_gateway": False,
            "serve_agent_runner": False,
            "serve_observability_station": True,
            "paused": False,
        },
    )
    from cli.commands.lifecycle.status import _print_gateway_cluster_status

    _print_gateway_cluster_status()
    out = capsys.readouterr().out
    assert "machine_name: station-a" in out
    assert "serves:       observability-station" in out
