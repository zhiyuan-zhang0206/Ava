"""Cluster roster, status transport, resume checklist, and hold banners; split from tests/components/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import pytest

import cli.commands.cluster.control as cluster_control
from cli.tests._commands_helpers import _fake_session_backends as _fake_session_backends
from cli.tests._commands_helpers import _FakeResponse
from cli.tests._commands_helpers import _gateway_role_pinned as _gateway_role_pinned
from cli.tests._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base

# ─── ava cluster status roster code column ───────────────────────────────────


def test_code_cell_matches_checkout() -> None:
    """running_sha == head_sha → the short SHA with no drift marker."""
    from cli.commands.cluster.control import _code_cell

    assert _code_cell(running_sha="abc1234def", head_sha="abc1234def") == "abc1234"


def test_code_cell_drift_marks_stale_process() -> None:
    """running_sha != head_sha → ⚠ + running short SHA (process running stale code
    vs its checkout)."""
    from cli.commands.cluster.control import _code_cell

    assert _code_cell(running_sha="999888777", head_sha="abc1234def") == "⚠ 9998887"


def test_code_cell_unknown_running_sha() -> None:
    """No running_sha recorded → em dash."""
    from cli.commands.cluster.control import _code_cell

    assert _code_cell(running_sha=None, head_sha="abc1234def") == "—"


def test_status_cell_identity_mismatch_outranks_online() -> None:
    """identity_mismatch renders a loud MISMATCH even when online is True — a
    wrong-identity responder is never shown as a plain online host."""
    from datetime import UTC, datetime

    from cli.commands.cluster.control import _status_cell

    stopped = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)
    assert _status_cell(online=True, identity_mismatch=True, stopped_at=None) == "MISMATCH"
    assert _status_cell(online=True, identity_mismatch=False, stopped_at=None) == "online"
    assert _status_cell(online=False, identity_mismatch=False, stopped_at=stopped) == "stopped"
    assert _status_cell(online=False, identity_mismatch=False, stopped_at=None) == "offline"
    # online + a stop marker is the two sources of truth disagreeing, not a green
    # host — see cli/commands/lifecycle/tests/test_rollout_robustness.py for why that mattered.
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
    rc = cluster_control.cmd_cluster_status()
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
    rc = cluster_control.cmd_cluster_status()
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
    rc = cluster_control.cmd_cluster_status()
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


# ─── ava cluster status transport failures report, they do not traceback ──────────


def test_cmd_cluster_status_read_timeout_reports_friendly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gateway that does not answer within the derived read budget prints one
    stderr line and exits 1. An unreachable machine is exactly when an operator
    runs `ava cluster status`; the derived budget already covers a black-holed
    machine's probe (probe budget + margin), so a timeout here means the
    gateway itself is silent — a bare ReadTimeout traceback would hide the
    diagnosis the command exists for (#219, #4900)."""
    import httpx

    from base.config import settings

    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 5.0)

    def _slow_get(url: str, **_kw: object) -> None:
        raise httpx.ReadTimeout("timed out", request=None)

    monkeypatch.setattr("httpx.get", _slow_get)  # pyright: ignore[reportUnknownArgumentType]
    rc = cluster_control.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "did not respond within 9s" in err
    assert "http://gw:8000/api/cluster/roster" in err


def test_roster_read_timeout_derives_from_the_probe_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The roster read budget tracks the gateway's per-machine probe budget at
    call time — a budget pinned at the probe budget collides with a black-holed
    machine's probe (#4900)."""
    from base.config import settings

    monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 5.0)
    assert cluster_control._roster_read_timeout_s() == 9.0


def test_cmd_cluster_status_dials_the_derived_read_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fresh roster dial carries the derived budget (probe budget + margin),
    not the thin-POST timeout constant (#4900)."""
    from base.config import settings

    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr(settings.gateway, "status_probe_timeout_seconds", 5.0)
    seen: dict[str, object] = {}

    def _fake_get(url: str, **kwargs: object) -> _FakeResponse:
        seen["url"] = url
        seen["timeout"] = kwargs.get("timeout")
        return _FakeResponse([])

    monkeypatch.setattr("httpx.get", _fake_get)  # pyright: ignore[reportUnknownArgumentType]
    assert cluster_control.cmd_cluster_status() == 0
    assert seen["url"] == "http://gw:8000/api/cluster/roster?fresh=true"
    assert seen["timeout"] == 9.0


def test_cmd_cluster_status_connect_error_reports_friendly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gateway that refuses the connection prints 'gateway unreachable' and
    exits 1 instead of raising (#219)."""
    import httpx

    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gw:8000")

    def _refused_get(url: str, **_kw: object) -> None:
        raise httpx.ConnectError("connection refused", request=None)

    monkeypatch.setattr("httpx.get", _refused_get)  # pyright: ignore[reportUnknownArgumentType]
    rc = cluster_control.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "gateway unreachable" in err
    assert "connection refused" in err


def test_cmd_cluster_status_http_error_reports_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-2xx roster response names the status code and exits 1 (#219)."""
    import httpx

    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gw:8000")

    def _server_error_get(url: str, **_kw: object) -> httpx.Response:
        # A real dial returns the 500 response; raise_for_status() in
        # cmd_cluster_status turns it into HTTPStatusError.
        request = httpx.Request("GET", url)
        return httpx.Response(500, request=request)

    monkeypatch.setattr("httpx.get", _server_error_get)  # pyright: ignore[reportUnknownArgumentType]
    rc = cluster_control.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 500" in err


def test_cmd_cluster_status_unresolvable_gateway_reports_friendly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A host that cannot resolve the gateway URL says why and exits 1 rather
    than raising GatewayApiBaseMissing (#219)."""
    from base.cluster.machine import GatewayApiBaseMissing

    monkeypatch.setattr(
        "base.cluster.machine.gateway_api_base",
        lambda: (_ for _ in ()).throw(GatewayApiBaseMissing("AVA_GATEWAY_URL unset")),
    )
    rc = cluster_control.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "cannot resolve gateway URL" in err


# ─── cmd_cluster_status (thin client over /api/cluster/roster) ────────────────


def _patch_roster_get(monkeypatch: pytest.MonkeyPatch, roster: list[dict]) -> list[str]:
    """Stub the gateway URL/headers + httpx.get so cmd_cluster_status renders `roster`."""
    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gw:8000")
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

    from base.api_contracts.status import MachineStatus

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
    rc = cluster_control.cmd_cluster_status()
    assert rc == 0
    assert calls == ["http://gw:8000/api/cluster/roster?fresh=true"]
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
    rc = cluster_control.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "test-host" in out and "online" in out
    assert "wsl" in out and "stopped" in out
    assert "corp" in out and "offline" in out


def test_cmd_cluster_status_fails_fast_on_http_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 5xx from the gateway exits 1 with the status named — fail-fast, no
    silent fallback, and no unhandled traceback (#219)."""
    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("httpx.get", lambda *_a, **_kw: _FakeResponse([], status_code=503))  # pyright: ignore[reportUnknownArgumentType]
    rc = cluster_control.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 503" in err


def test_cmd_cluster_status_without_held_hosts_has_no_banner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_roster_get(monkeypatch, [_machine_row(name="wsl")])
    rc = cluster_control.cmd_cluster_status()
    assert rc == 0
    assert "host left held" not in capsys.readouterr().out


# ─── ava cluster resume machine-side checklist ───────────────────────────────


# ─── ava cluster pause / resume / staging wording ────────────────────────────


def _post_returns(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> None:
    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("base.cluster.machine.gateway_auth_headers", dict)
    monkeypatch.setattr(
        "base.host.net.http_dial.post",
        lambda *_a, **_kw: _FakeResponse(payload),  # pyright: ignore[reportUnknownArgumentType]
    )


def test_pause_names_what_a_paused_machine_is_hidden_from(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No rollout exists any more; a pause hides the machine from the roster,
    the probe, cluster fan-outs and spawn."""
    _post_returns(
        monkeypatch,
        {
            "paused_at": "2026-09-27T00:00:00+00:00",
            "pause_reason": "",
            "terminated_agents": 0,
            "force_marked_agents": 0,
            "reassigned_tasks": 0,
        },
    )
    assert cluster_control.cmd_cluster_pause("wsl") == 0
    out = capsys.readouterr().out
    assert "hidden from roster/probe/fan-out/spawn until resumed" in out
    assert "rollout" not in out


def test_resume_names_what_is_restored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _post_returns(monkeypatch, {"name": "wsl", "resumed": True})
    assert cluster_control.cmd_cluster_resume("wsl") == 0
    out = capsys.readouterr().out
    assert "wsl: resumed — probing / roster / fan-out / spawn restored" in out
    assert "rollout" not in out


def test_unmark_staging_names_the_fan_out_target_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _post_returns(monkeypatch, {"deleted": True})
    assert cluster_control.cmd_cluster_mark_staging("wsl", is_staging=False) == 0
    assert capsys.readouterr().out == "wsl: unmarked staging (now a fan-out target)\n"
