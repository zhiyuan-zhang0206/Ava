"""Agent-profile service sessions receive runner database credentials."""

from __future__ import annotations

from pathlib import Path

import pytest

import cli.commands._session_lifecycle as lifecycle
from cli import commands
from ops.service_spec import _AGENT_RUNNER, ServiceSpec
from shared.machine import MachineRoles


def test_launch_roster_projects_runner_url_for_agent_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spec = ServiceSpec(
        session="agent-host",
        cmd=".venv/bin/python -m services.agent_host.daemon",
        capabilities=_AGENT_RUNNER,
        requires_db=True,
        profile="agent",
    )
    captured: list[dict[str, str] | None] = []

    def _roster(_roles: MachineRoles, _skip: set[str]) -> tuple[ServiceSpec, ...]:
        return (spec,)

    def _has_session(_session: str) -> bool:
        return False

    def _project(_url: str) -> str:
        return "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava"

    monkeypatch.setattr(lifecycle, "_launch_roster", _roster)
    monkeypatch.setattr(commands, "_has_session", _has_session)
    monkeypatch.setattr(
        lifecycle,
        "runner_db_url_projection",
        _project,
    )

    def _new_session(
        _session: str, _cmd: str, _cwd: Path, *, extra_env: dict[str, str] | None = None
    ) -> bool:
        captured.append(extra_env)
        return True

    monkeypatch.setattr(commands, "_new_session", _new_session)

    lifecycle._launch_sessions(frozenset({"agent-runner"}), set(), tmp_path)

    assert captured == [
        {
            "AVA_PROCESS_PROFILE": "agent",
            "AVA_DB_URL": "postgresql://ava_runner:runner-password@127.0.0.1:5433/ava",
        }
    ]
