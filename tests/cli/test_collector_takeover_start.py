"""The start supervisor performs collector stale takeover before launching."""

from __future__ import annotations

from pathlib import Path

import pytest

import cli.commands._session_lifecycle as lifecycle
from cli import commands
from ops.spec import _BOTH, ServiceSpec
from shared.machine import MachineRoles


def test_start_reclaims_a_stale_collector_before_launching_its_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The initial supervisor and the watchdog share the collector takeover action."""
    events: list[str] = []
    spec = ServiceSpec(
        session="otel-collector",
        cmd="otelcol-contrib --config config.yaml",
        capabilities=_BOTH,
        requires_db=False,
        before_launch=lambda: events.append("take-over"),
    )

    def _roster(_roles: MachineRoles, _skip: set[str]) -> tuple[ServiceSpec, ...]:
        return (spec,)

    def _has_session(_session: str) -> bool:
        return False

    def _new_session(
        _session: str, _cmd: str, _cwd: Path, *, extra_env: dict[str, str] | None = None
    ) -> bool:
        del extra_env
        events.append("launch")
        return True

    monkeypatch.setattr(lifecycle, "_launch_roster", _roster)
    monkeypatch.setattr(commands, "_has_session", _has_session)
    monkeypatch.setattr(commands, "_new_session", _new_session)

    lifecycle._launch_sessions(frozenset({"gateway"}), set(), tmp_path)

    assert events == ["take-over", "launch"]
