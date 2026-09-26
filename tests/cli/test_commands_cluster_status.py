"""Cluster roster, status transport, resume checklist, and hold banners; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import pytest

import cli.commands.cluster as _cluster_commands
from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import _FakeResponse
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base
from tests.cli._commands_helpers import _noop_start_prechecks as _noop_start_prechecks

# ─── ava cluster status roster code column ───────────────────────────────────


def test_code_cell_matches_checkout() -> None:
    """running_sha == head_sha → the short SHA with no drift marker."""
    from cli.commands.cluster import _code_cell

    assert _code_cell(running_sha="abc1234def", head_sha="abc1234def") == "abc1234"


def test_code_cell_drift_marks_stale_process() -> None:
    """running_sha != head_sha → ⚠ + running short SHA (process running stale code
    vs its checkout)."""
    from cli.commands.cluster import _code_cell

    assert _code_cell(running_sha="999888777", head_sha="abc1234def") == "⚠ 9998887"


def test_code_cell_unknown_running_sha() -> None:
    """No running_sha recorded → em dash."""
    from cli.commands.cluster import _code_cell

    assert _code_cell(running_sha=None, head_sha="abc1234def") == "—"


def test_status_cell_identity_mismatch_outranks_online() -> None:
    """identity_mismatch renders a loud MISMATCH even when online is True — a
    wrong-identity responder is never shown as a plain online host."""
    from datetime import UTC, datetime

    from cli.commands.cluster import _status_cell

    stopped = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)
    assert _status_cell(online=True, identity_mismatch=True, stopped_at=None) == "MISMATCH"
    assert _status_cell(online=True, identity_mismatch=False, stopped_at=None) == "online"
    assert _status_cell(online=False, identity_mismatch=False, stopped_at=stopped) == "stopped"
    assert _status_cell(online=False, identity_mismatch=False, stopped_at=None) == "offline"
    # online + a stop marker is the two sources of truth disagreeing, not a green
    # host — see tests/cli/test_rollout_robustness.py for why that mattered.
    assert _status_cell(online=True, identity_mismatch=False, stopped_at=stopped) == "STALE-STOP"


def test_cmd_cluster_status_renders_identity_mismatch_and_code_drift(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A row flagged identity_mismatch shows MISMATCH; a row whose running_sha
    differs from head_sha shows the ⚠ drift marker in the new `code` column."""
    roster = [
        _machine_row(
            name="impostor",
            serve_gateway=False,
            serve_agent_runner=True,
            online=False,
            identity_mismatch=True,
        ),
        _machine_row(
            name="stale",
            serve_gateway=False,
            serve_agent_runner=True,
            head_sha="abc1234def",
            running_sha="999888777",
        ),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "code" in out  # new column header
    assert "MISMATCH" in out
    assert "⚠ 9998887" in out


def test_cmd_cluster_status_renders_role_column_without_a_pin_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The roster has a `role` column derived from the serve_gateway /
    serve_agent_runner / serve_observability_station capability flags
    (regression for the KeyError('role') crash) and no `pin` column: nothing
    writes the cluster pin, so a verdict against it would be a stale value
    presented as current."""
    roster = [
        _machine_row(
            name="cloud",
            serve_gateway=True,
            serve_agent_runner=True,
            head_sha="abc1234def",
            running_sha="abc1234def",
        ),
        _machine_row(
            name="wsl",
            serve_gateway=False,
            serve_agent_runner=True,
            head_sha="999888777",
            running_sha="999888777",
        ),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    header = out.splitlines()[0].split()
    assert "pin" not in header and "code" in header
    assert "✓" not in out and "✗" not in out
    cloud_line = next(line for line in out.splitlines() if line.startswith("cloud"))
    wsl_line = next(line for line in out.splitlines() if line.startswith("wsl"))
    assert "gateway + agent-runner" in cloud_line
    assert "agent-runner" in wsl_line and "gateway +" not in wsl_line


def test_cmd_cluster_status_role_column_shows_observability_station(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A pure observability-station host renders as "observability-station" in
    the roster's role column (WP2 — previously the third capability flag was
    not on the wire and such a host read as "none")."""
    roster = [
        _machine_row(
            name="station-a",
            serve_gateway=False,
            serve_agent_runner=False,
            serve_observability_station=True,
        ),
        _machine_row(
            name="combo",
            serve_gateway=True,
            serve_agent_runner=False,
            serve_observability_station=True,
        ),
        _machine_row(
            name="runner-a",
            serve_gateway=False,
            serve_agent_runner=True,
            serve_observability_station=False,
        ),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    station_line = next(line for line in out.splitlines() if line.startswith("station-a"))
    combo_line = next(line for line in out.splitlines() if line.startswith("combo"))
    runner_line = next(line for line in out.splitlines() if line.startswith("runner-a"))
    assert "observability-station" in station_line and "gateway" not in station_line
    assert "gateway + observability-station" in combo_line
    # Zero regression: a pure runner row never picks up the station token.
    assert "agent-runner" in runner_line
    assert "observability-station" not in runner_line


def test_cmd_status_gateway_cluster_serves_line_shows_station(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava status`'s gateway cluster-status supplement renders the station
    capability in the serves: line when the gateway snapshot carries it
    (the function imports _fetch_gateway_cluster_status at call time, so the
    module attribute patch is the rebind that takes effect)."""
    monkeypatch.setattr(
        "cli.commands.cluster._fetch_gateway_cluster_status",
        lambda: {
            "machine_name": "station-a",
            "serve_gateway": False,
            "serve_agent_runner": False,
            "serve_observability_station": True,
            "paused": False,
        },
    )
    from cli.commands.status import _print_gateway_cluster_status

    _print_gateway_cluster_status()
    out = capsys.readouterr().out
    assert "machine_name: station-a" in out
    assert "serves:       observability-station" in out


# ─── ava cluster status transport failures report, they do not traceback ──────────


def test_cmd_cluster_status_read_timeout_reports_friendly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gateway that does not answer within the probe budget prints one stderr
    line and exits 1. An unreachable machine is exactly when an operator runs
    `ava cluster status`, and its roster probe can push the gateway's own
    response past this client's budget — so a bare ReadTimeout traceback would
    hide the diagnosis the command exists for (#219)."""
    import httpx

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    def _slow_get(url: str, **_kw: object) -> None:
        raise httpx.ReadTimeout("timed out", request=None)

    monkeypatch.setattr("httpx.get", _slow_get)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "did not respond within" in err
    assert "http://gw:8000/api/cluster/roster" in err


def test_cmd_cluster_status_connect_error_reports_friendly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gateway that refuses the connection prints 'gateway unreachable' and
    exits 1 instead of raising (#219)."""
    import httpx

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    def _refused_get(url: str, **_kw: object) -> None:
        raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr("httpx.get", _refused_get)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "gateway unreachable" in err
    assert "connection refused" in err


def test_cmd_cluster_status_http_error_reports_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-2xx roster response names the status code and exits 1 (#219)."""
    import httpx

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")

    def _server_error_get(url: str, **_kw: object) -> httpx.Response:
        # A real dial returns the 500 response; raise_for_status() in
        # cmd_cluster_status turns it into HTTPStatusError.
        request = httpx.Request("GET", url)
        return httpx.Response(500, request=request)

    monkeypatch.setattr("httpx.get", _server_error_get)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 500" in err


def test_cmd_cluster_status_unresolvable_gateway_reports_friendly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A host that cannot resolve the gateway URL says why and exits 1 rather
    than raising GatewayApiBaseMissing (#219)."""
    from shared.machine import GatewayApiBaseMissing

    monkeypatch.setattr(
        "shared.machine.gateway_api_base",
        lambda: (_ for _ in ()).throw(GatewayApiBaseMissing("AVA_GATEWAY_URL unset")),
    )
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot resolve gateway URL" in err


# ─── cmd_cluster_status (thin client over /api/cluster/roster) ────────────────


def _patch_roster_get(monkeypatch: pytest.MonkeyPatch, roster: list[dict]) -> list[str]:
    """Stub the gateway URL/headers + httpx.get so cmd_cluster_status renders `roster`."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    calls: list[str] = []

    def _fake_get(url, **_kw):
        calls.append(url)  # pyright: ignore[reportUnknownArgumentType]
        return _FakeResponse(roster)

    monkeypatch.setattr("httpx.get", _fake_get)  # pyright: ignore[reportUnknownArgumentType]
    return calls


def _machine_row(**overrides: object) -> dict[str, object]:
    """A real MachineStatus serialized to its wire dict, with field overrides.

    Building rows from the actual schema (not a hand-written dict) keeps the
    roster tests honest: if MachineStatus drops/renames a field the renderer
    reads, they fail here instead of drifting silently — which is exactly how the
    KeyError('role') crash shipped (the old fixtures carried a `role` field the
    wire schema no longer has).
    """
    from datetime import UTC, datetime

    from gateway.schemas import MachineStatus

    base = MachineStatus(
        name="test-host",
        serve_gateway=True,
        serve_agent_runner=True,
        gateway_url="http://gw:8000",
        up_since_at=datetime(2026, 6, 1, 7, 0, tzinfo=UTC),
        online=True,
        paused=False,
    )
    return base.model_copy(update=overrides).model_dump(mode="json")


def test_cmd_cluster_status_empty_roster_prints_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Empty roster from the gateway -> hint + exit 0."""
    calls = _patch_roster_get(monkeypatch, [])
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 0
    assert calls == ["http://gw:8000/api/cluster/roster"]
    assert "machines table empty" in capsys.readouterr().out


def test_cmd_cluster_status_renders_online_stopped_offline(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The roster's online / stopped_at fields render as online / stopped / offline."""
    from datetime import UTC, datetime

    roster = [
        _machine_row(name="test-host", online=True, stopped_at=None),
        _machine_row(
            name="wsl",
            online=False,
            paused=None,
            stopped_at=datetime(2026, 6, 1, 6, 0, tzinfo=UTC),
        ),
        _machine_row(name="corp", online=False, paused=None, stopped_at=None),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "test-host" in out and "online" in out
    assert "wsl" in out and "stopped" in out
    assert "corp" in out and "offline" in out


def test_cmd_cluster_status_renders_the_deploy_hold_banner_without_a_hold_column(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A live deploy lease shows as the banner naming it (the answer to "why was
    my deploy refused"). There is no per-host `hold` column: nothing records a
    settle hold's waiting set any more, so a per-host verdict would be a dead
    state."""
    hold = "machine-1:pid42 (held 5m, lease expires in 10m)"
    roster = [
        _machine_row(name="test-host", deploy_hold=hold),
        _machine_row(name="wsl", deploy_hold=hold),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"deploy hold: {hold}"
    # The banner states the operator-visible consequence, which is what brought them here.
    assert any("no other owner can take the cluster deploy lease" in line for line in out[:5])
    header = next(line for line in out if line.startswith("name"))
    assert "hold" not in header.split()
    assert not any("waited-on" in line for line in out)


def test_cmd_cluster_status_prints_no_banner_when_no_hold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No live lease -> no banner at all. An absent lease is not evidence the
    cluster is free (host-local maintenance takes no cluster lease), so the roster
    does not claim it is."""
    _patch_roster_get(monkeypatch, [_machine_row(name="test-host")])
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "deploy hold" not in out
    assert out.splitlines()[0].startswith("name")


def test_cmd_cluster_status_fails_fast_on_http_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 5xx from the gateway exits 1 with the status named — fail-fast, no
    silent fallback, and no unhandled traceback (#219)."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.get", lambda *_a, **_kw: _FakeResponse([], status_code=503))  # pyright: ignore[reportUnknownArgumentType]
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 503" in err


def test_cmd_cluster_status_without_held_hosts_has_no_banner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_roster_get(monkeypatch, [_machine_row(name="wsl")])
    rc = _cluster_commands.cmd_cluster_status()
    assert rc == 0
    assert "host left held" not in capsys.readouterr().out


# ─── ava cluster resume machine-side checklist ───────────────────────────────


def test_cmd_cluster_resume_checklist_names_only_commands_that_parse(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every `ava ...` command the resume checklist prints must parse with the real
    CLI parser — an operator following it after an address change is exactly the
    person who cannot afford a step that exits 2. The pg_hba step names the
    current regeneration path: a gateway `ava restart` rewrites pg_hba.conf and
    reloads the retained Postgres on its start leg."""
    import re

    from cli.parsers import build_parser

    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", dict)
    monkeypatch.setattr(
        "shared.http_dial.post",
        lambda *_a, **_kw: _FakeResponse({"name": "wsl", "resumed": True}),
    )
    assert _cluster_commands.cmd_cluster_resume("wsl") == 0
    out = capsys.readouterr().out
    commands = re.findall(r"`(ava [^`]+)`", out)
    assert commands, out
    parser = build_parser()
    for command in commands:
        parser.parse_args(command.split()[1:])  # SystemExit(2) fails the test
    assert "`ava restart`" in out
    assert "--restart-only" not in out
