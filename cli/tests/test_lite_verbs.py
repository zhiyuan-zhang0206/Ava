"""Which verbs `cli.main` opts out of the gateway config fetch (settings-lite).

The lite opt-out exists so a runner's recovery verbs still construct Settings
with the gateway down. `restart` must NOT be one of them: its preflight
registers this machine in the central DB and its start leg needs the cluster
config regardless, so a lite restart on a pure runner could only ever dial the
never-dialed placeholder DB URL and die with PlaceholderDbUrlError (observed on
the fleet Windows box, 2026-08-19) — while a fetching restart works whenever
restart can work at all.

The dispatch is exercised for real (`cli.main.main`), with only the parser
stubbed, so the test pins the actual env the dispatched verb runs under.
"""

from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase
from cli import main as cli_main


def _dispatched_fetch_env(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> str | None:
    """Run `cli.main.main(argv)` with a stub parser; return the AVA_CONFIG_FETCH
    value the dispatched verb's handler observed."""
    seen: dict[str, str | None] = {}

    def handler(_args: types.SimpleNamespace) -> int:
        seen["fetch"] = os.environ.get("AVA_CONFIG_FETCH")
        return 0

    parser = types.SimpleNamespace(parse_args=lambda _argv: types.SimpleNamespace(func=handler))
    monkeypatch.setattr(cli_main, "_build_parser", lambda: parser)
    monkeypatch.delenv("AVA_CONFIG_FETCH", raising=False)
    assert cli_main.main(argv) == 0
    return seen["fetch"]


def test_restart_is_not_settings_lite(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _dispatched_fetch_env(monkeypatch, ["restart"]) is None


def test_normal_drain_requires_cluster_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native restart/checkpoint draining needs the runner's real data-plane URLs."""
    assert _dispatched_fetch_env(monkeypatch, ["stop"]) is None


def test_force_stop_stays_settings_lite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit force remains available when the gateway cannot supply config."""
    assert _dispatched_fetch_env(monkeypatch, ["stop", "--force"]) == "skip"


@pytest.mark.parametrize(
    ("state", "hold", "expected"),
    [
        ("paused", MaintenanceHold(MaintenancePhase.STOPPED), "skip"),
        ("paused", MaintenanceHold(MaintenancePhase.DRAINED), None),
        (
            "paused",
            MaintenanceHold(MaintenancePhase.STOPPED, failures={1: "checkpoint_flush"}),
            None,
        ),
        ("resumed", MaintenanceHold(MaintenancePhase.STOPPED), None),
        ("paused", None, None),
    ],
)
def test_only_completed_cold_stop_can_skip_gateway_fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
    hold: MaintenanceHold | None,
    expected: str | None,
) -> None:
    """A repeated stop works offline; incomplete or failed work must still drain."""
    # Bootstrap and spawned interpreters consume the raw home before Settings.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(tmp_path))
    journal = tmp_path / "run" / "deploy-pause-owner.json"
    journal.parent.mkdir()
    payload: dict[str, object] = {
        "state": state,
        "holder": "local-pause:test",
        "acquired_at": "2026-09-07T00:00:00Z",
    }
    if hold is not None:
        payload["maintenance"] = hold.encode()
    journal.write_text(json.dumps(payload))
    before = journal.read_bytes()
    assert _dispatched_fetch_env(monkeypatch, ["stop"]) == expected
    assert journal.read_bytes() == before


def test_corrupt_stop_journal_is_not_offline_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(os.environ, "AVA_HOME", str(tmp_path))
    journal = tmp_path / "run" / "deploy-pause-owner.json"
    journal.parent.mkdir()
    journal.write_text('{"state":"paused","maintenance":')
    before = journal.read_bytes()
    assert _dispatched_fetch_env(monkeypatch, ["stop"]) is None
    assert journal.read_bytes() == before


def test_pty_stays_settings_lite(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allocation gate is an offline host recovery surface."""
    assert _dispatched_fetch_env(monkeypatch, ["pty", "status"]) == "skip"


def test_status_stays_settings_lite(monkeypatch: pytest.MonkeyPatch) -> None:
    """`status` (and `status --json`, the hold's reader) reads the local journal first,
    so it stays offline-capable."""
    assert _dispatched_fetch_env(monkeypatch, ["status"]) == "skip"
    assert _dispatched_fetch_env(monkeypatch, ["status", "--json"]) == "skip"


@pytest.mark.parametrize(
    ("explicit", "capability_token", "expected"),
    [
        ("explicit-token", "runner-token", "explicit-token"),
        (None, "runner-token", "runner-token"),
        (None, None, None),
    ],
)
def test_lite_cli_uses_its_local_runner_api_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    explicit: str | None,
    capability_token: str | None,
    expected: str | None,
) -> None:
    from base.cluster.authority import AuthorityRefusedError, unit
    from base.cluster.machine import gateway_auth_headers
    from base.config import settings

    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    monkeypatch.delenv("AVA_LAUNCHER_PROFILE", raising=False)
    monkeypatch.delenv("AVA_PROCESS_PROFILE", raising=False)
    monkeypatch.setenv("AVA_CONFIG_FETCH", "")
    monkeypatch.delenv("AVA_CONFIG_FETCH", raising=False)
    if explicit is None:
        monkeypatch.setenv("AVA_API_TOKEN", "")
        monkeypatch.delenv("AVA_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("AVA_API_TOKEN", explicit)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", lambda: False)

    def consume(home: Path) -> types.SimpleNamespace:
        assert home == tmp_path
        if capability_token is None:
            raise AuthorityRefusedError("no local capability")
        return types.SimpleNamespace(api=types.SimpleNamespace(token=capability_token))

    monkeypatch.setattr(unit, "consume_unit", consume)
    seen: list[dict[str, str]] = []

    def handler(_args: types.SimpleNamespace) -> int:
        seen.append(gateway_auth_headers())
        return 0

    def parse_args(_argv: list[str] | None) -> types.SimpleNamespace:
        return types.SimpleNamespace(func=handler)

    parser = types.SimpleNamespace(parse_args=parse_args)
    monkeypatch.setattr(cli_main, "_build_parser", lambda: parser)

    assert cli_main.main(["agents"]) == 0
    assert seen == ([{}] if expected is None else [{"Authorization": f"Bearer {expected}"}])


def test_lite_cli_in_agent_profile_does_not_consume_operator_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from base.cluster.authority import unit

    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    monkeypatch.setenv("AVA_PROCESS_PROFILE", "agent")
    monkeypatch.setenv("AVA_LAUNCHER_PROFILE", "")
    monkeypatch.delenv("AVA_LAUNCHER_PROFILE", raising=False)
    monkeypatch.delenv("AVA_API_TOKEN", raising=False)
    monkeypatch.setenv("AVA_CONFIG_FETCH", "")
    monkeypatch.delenv("AVA_CONFIG_FETCH", raising=False)
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", lambda: False)

    def no_operator_capability(_home: Path) -> None:
        pytest.fail("an agent descendant must retain the launcher token boundary")

    monkeypatch.setattr(unit, "consume_unit", no_operator_capability)

    def handler(_args: types.SimpleNamespace) -> int:
        return 0

    def parse_args(_argv: list[str] | None) -> types.SimpleNamespace:
        return types.SimpleNamespace(func=handler)

    parser = types.SimpleNamespace(parse_args=parse_args)
    monkeypatch.setattr(cli_main, "_build_parser", lambda: parser)

    assert cli_main.main(["agents"]) == 0
    assert "AVA_API_TOKEN" not in os.environ
