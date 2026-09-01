"""Which verbs `cli.main` opts out of the gateway config fetch (settings-lite).

The lite opt-out exists so a runner's recovery verbs still construct Settings
with the gateway down. `restart` must NOT be one of them: its preflight
registers this machine in the central DB and its start leg needs the cluster
config regardless, so a lite restart on a pure runner could only ever dial the
never-dialed placeholder DB URL and die with UnanchoredHomeError (observed on
the fleet Windows box, 2026-08-19) — while a fetching restart works whenever
restart can work at all.

The dispatch is exercised for real (`cli.main.main`), with only the parser
stubbed, so the test pins the actual env the dispatched verb runs under.
"""

from __future__ import annotations

import os
import types

import pytest

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


def test_stop_stays_settings_lite(monkeypatch: pytest.MonkeyPatch) -> None:
    """`stop` is the recovery verb and must keep working with the gateway down."""
    assert _dispatched_fetch_env(monkeypatch, ["stop"]) == "skip"


def test_pty_stays_settings_lite(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allocation gate is an offline host recovery surface."""
    assert _dispatched_fetch_env(monkeypatch, ["pty", "status"]) == "skip"
