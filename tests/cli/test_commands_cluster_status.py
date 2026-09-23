"""Cluster roster, status transport, restart, and hold banners; split from tests/cli/test_commands.py (task #4554)."""

from __future__ import annotations

import pytest

from cli import commands as _cli
from tests.cli._commands_helpers import _fake_session_backends as _fake_session_backends
from tests.cli._commands_helpers import _FakeResponse
from tests.cli._commands_helpers import _hermetic_gateway_base as _hermetic_gateway_base
from tests.cli._commands_helpers import _noop_start_prechecks as _noop_start_prechecks

# ─── ava cluster status roster pin column ────────────────────────────────────


def test_pin_cell_on_pin() -> None:
    from cli.commands.cluster import _pin_cell

    assert _pin_cell(on_pin=True, head_sha="abc1234def") == "✓ abc1234"


def test_pin_cell_off_pin() -> None:
    from cli.commands.cluster import _pin_cell

    assert _pin_cell(on_pin=False, head_sha="abc1234def") == "✗ abc1234"


def test_pin_cell_unknown() -> None:
    from cli.commands.cluster import _pin_cell

    assert _pin_cell(None, None) == "? —"


def test_code_cell_matches_checkout() -> None:
    """running_sha == head_sha → the short SHA with no drift marker."""
    from cli.commands.cluster import _code_cell

    assert _code_cell(running_sha="abc1234def", head_sha="abc1234def") == "abc1234"


def test_code_cell_drift_marks_stale_process() -> None:
    """running_sha != head_sha → ⚠ + running short SHA (process running stale code
    vs its checkout, even when pin reads ✓)."""
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
            on_pin=True,
            head_sha="abc1234def",
            running_sha="999888777",
        ),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "code" in out  # new column header
    assert "MISMATCH" in out
    assert "⚠ 9998887" in out


def test_cmd_cluster_status_renders_pin_and_role_columns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The roster has a `pin` column (✓/✗ from each row's on_pin) and a `role`
    column derived from the serve_gateway / serve_agent_runner /
    serve_observability_station capability flags
    (regression for the KeyError('role') crash)."""
    roster = [
        _machine_row(
            name="cloud",
            serve_gateway=True,
            serve_agent_runner=True,
            on_pin=True,
            head_sha="abc1234def",
        ),
        _machine_row(
            name="wsl",
            serve_gateway=False,
            serve_agent_runner=True,
            on_pin=False,
            head_sha="999888777",
        ),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "pin" in out
    assert "✓ abc1234" in out
    assert "✗ 9998887" in out
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
    rc = _cli.cmd_cluster_status()
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
    rc = _cli.cmd_cluster_status()
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
    rc = _cli.cmd_cluster_status()
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
    rc = _cli.cmd_cluster_status()
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
    rc = _cli.cmd_cluster_status()
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
    rc = _cli.cmd_cluster_status()
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
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "test-host" in out and "online" in out
    assert "wsl" in out and "stopped" in out
    assert "corp" in out and "offline" in out


def test_cmd_cluster_status_renders_hold_column_and_banner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A live settle hold shows up twice: the banner naming the lease (the answer to
    "why was my deploy refused"), and `waited-on` on exactly the hosts the hold's
    recorded set names — every other host reads `—`, which is "not named", not
    "converged"."""
    roster = [
        _machine_row(
            name="test-host",
            deploy_hold="machine-1:pid42 (held 5m, lease expires in 10m) — settling, waiting for: wsl",
            settle_waited_on=False,
        ),
        _machine_row(
            name="wsl",
            deploy_hold="machine-1:pid42 (held 5m, lease expires in 10m) — settling, waiting for: wsl",
            settle_waited_on=True,
        ),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        "deploy hold: machine-1:pid42 (held 5m, lease expires in 10m) — settling, waiting for: wsl"
    )
    # The banner states the operator-visible consequence, which is what brought them here.
    assert any("auto-rollback" in line for line in out[:5])
    header = next(line for line in out if line.startswith("name"))
    assert "hold" in header
    wsl_row = next(line for line in out if line.startswith("wsl"))
    assert "waited-on" in wsl_row
    local_row = next(line for line in out if line.startswith("test-host"))
    assert "waited-on" not in local_row


def _failed_update(**overrides: object) -> object:
    """A real LastUpdate, as the roster stamps it onto every row. Handed to
    `_machine_row` as the model so MachineStatus serializes it — a hand-built dict
    would validate against nothing and drift silently."""
    from datetime import UTC, datetime

    from shared.last_update import LastUpdate, UpdateOutcome

    base = LastUpdate(
        outcome=UpdateOutcome.INCOMPLETE,
        failed=True,
        target_sha="8bdd3667aa",
        origin="frontend",
        started_at=datetime(2026, 7, 30, 21, 10, tzinfo=UTC),
        failing_step="the gateway was not serving, so Phase B never fanned out",
    )
    return base.model_copy(update=overrides)


def test_cmd_cluster_status_states_a_failed_update_instead_of_leaving_a_sha_riddle(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The roster's `pin` / `code` cells are symptoms — a node that missed a rollout
    and a rollout that failed and rolled back produce the same mismatch. The banner
    states which, above everything else, with the step and whether the pin moved."""
    roster = [_machine_row(name="test-host", last_update=_failed_update())]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    out = capsys.readouterr().out.splitlines()
    assert "FAILED" in out[0] and "8bdd366" in out[0]
    top = "\n".join(out[:5])
    assert "Phase B never fanned out" in top
    assert "pin was left where it was" in top
    assert "next successful" in top.lower() or "next successful" in "\n".join(out[:6])


def test_cmd_cluster_status_shows_the_rollback_anchor_and_the_recovery_that_ran(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`last_known_good_sha` was recorded since the pin existed and shown nowhere, so
    a rollback presented as the pin moving backwards for no stated reason. With the
    anchor and the observer's own sentence, the pin change reads as the designed
    fallback it is."""
    roster = [
        _machine_row(
            name="test-host",
            cluster_last_known_good_sha="7e571b49aa",
            last_update=_failed_update(observed_by="rolled back 8bdd366 -> 7e571b4"),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    top = "\n".join(capsys.readouterr().out.splitlines()[:6])
    assert "since then: rolled back 8bdd366 -> 7e571b4" in top
    assert "rollback anchor (last known good): 7e571b4" in top


def test_cmd_cluster_status_dates_the_failure_so_a_stale_one_reads_as_stale(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Minutes old is a live incident; days old is a cluster nobody has updated
    since. The banner has to let those two be told apart at a glance."""
    from datetime import UTC, datetime, timedelta

    roster = [
        _machine_row(
            name="test-host",
            last_update=_failed_update(started_at=datetime.now(UTC) - timedelta(days=3)),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    assert "(3d ago)" in capsys.readouterr().out


def test_cmd_cluster_status_says_nothing_about_an_update_that_succeeded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silent on success by design: a permanent "last update: ok" line is one people
    stop reading, and this is the line that has to be read the once it appears."""
    from shared.last_update import UpdateOutcome

    roster = [
        _machine_row(
            name="test-host",
            last_update=_failed_update(outcome=UpdateOutcome.CLEAN, failed=False),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("name"), "a successful update must add no banner at all"


def test_cmd_cluster_status_marks_a_recovered_update_as_a_warning_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A recovery still has to be stated — that silence is the 2026-07-30 bug — but
    it is not the same call to action as an unhandled failure, so it does not get the
    same glyph. Giving both `✗` is how the actionable one stops being read."""
    from shared.last_update import UpdateOutcome

    roster = [
        _machine_row(
            name="test-host",
            last_update=_failed_update(
                outcome=UpdateOutcome.RECOVERED,
                observed_by="rolled back 8bdd366 -> 7e571b4",
            ),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("⚠"), f"a recovered update must not read as a live failure: {out[0]}"
    assert "RECOVERED" in out[0]


def test_cmd_cluster_status_names_the_rollouts_own_log_when_the_record_has_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The record carries the log the run was actually writing, so the banner points
    at that file instead of at the glob an operator then has to pick from by mtime."""
    roster = [
        _machine_row(
            name="test-host",
            last_update=_failed_update(log_path="/home/ava/.ava/logs/rollout-1785470000.log"),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    top = "\n".join(capsys.readouterr().out.splitlines()[:6])
    assert "/home/ava/.ava/logs/rollout-1785470000.log" in top
    assert "rollout-<epoch>.log" not in top


def test_cmd_cluster_status_falls_back_to_the_log_pattern_when_none_was_recorded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A foreground `ava cluster update --local` has no log of its own. Naming a specific
    file there would be a guess, so the banner describes where rollout logs live."""
    roster = [_machine_row(name="test-host", last_update=_failed_update(log_path=None))]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    assert "rollout-<epoch>.log" in capsys.readouterr().out


def test_cmd_cluster_status_reports_an_orphaned_update_as_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reading no process could have filed for itself: the orchestration died,
    so the record was never closed and the lease it held is gone."""
    from shared.last_update import UpdateOutcome

    roster = [
        _machine_row(
            name="test-host",
            last_update=_failed_update(outcome=UpdateOutcome.ORPHANED, failing_step=None),
        )
    ]
    _patch_roster_get(monkeypatch, roster)

    assert _cli.cmd_cluster_status() == 0

    assert "died without reporting an outcome" in capsys.readouterr().out


def test_cmd_cluster_status_prints_no_banner_when_no_hold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No live lease -> no banner at all. A blank `hold` column is not evidence the
    cluster is free (a host-local watchdog update takes no lease), so the roster does
    not claim it is."""
    _patch_roster_get(monkeypatch, [_machine_row(name="test-host")])
    rc = _cli.cmd_cluster_status()
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
    rc = _cli.cmd_cluster_status()
    assert rc == 1
    err = capsys.readouterr().err
    assert "HTTP 503" in err


def test_cmd_cluster_restart_posts_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava cluster restart` POSTs /api/cluster/restart and reports the updater session."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    calls: list[str] = []

    def _fake_post(url, **_kw):
        calls.append(url)  # pyright: ignore[reportUnknownArgumentType]
        return _FakeResponse({"session": "ava-updater", "log": "/var/log/u.log"})

    monkeypatch.setattr("httpx.post", _fake_post)  # pyright: ignore[reportUnknownArgumentType]
    rc = _cli.cmd_cluster_restart()
    assert rc == 0
    assert calls == ["http://gw:8000/api/cluster/restart"]
    assert "ava-updater" in capsys.readouterr().out


def test_cmd_cluster_status_renders_stranded_hold_banner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A host carrying a stranded-hold record renders the held-host banner — its
    reason and the one-command remedy — above the table (task #3132)."""
    from datetime import UTC, datetime

    roster = [
        _machine_row(
            name="macmini",
            online=False,
            paused=None,
            stranded_hold_since=datetime(2026, 9, 12, 1, 2, tzinfo=UTC),
            stranded_hold_reason="updater exited rc=1",
        ),
        _machine_row(name="wsl"),
    ]
    _patch_roster_get(monkeypatch, roster)
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    out = capsys.readouterr().out
    assert "macmini: update failed (updater exited rc=1) \u2014 host left held" in out
    assert "run `ava start` on macmini" in out
    # banner above the table header
    assert out.index("host left held") < out.index("up since")


def test_cmd_cluster_status_without_held_hosts_has_no_banner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_roster_get(monkeypatch, [_machine_row(name="wsl")])
    rc = _cli.cmd_cluster_status()
    assert rc == 0
    assert "host left held" not in capsys.readouterr().out
