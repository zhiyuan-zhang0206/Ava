"""`cli.commands._warmup._launch_agent_warmup` unit tests.

Covers the role gate, the detached Popen argv/kwargs, and the best-effort
swallow on a launch failure. No real process is forked — `subprocess.Popen` is
monkeypatched. These opt out of the autouse warm-up guard (conftest) since they
exercise the real launcher directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cli.commands import _warmup

pytestmark = pytest.mark.real_warmup_launch


def test_skipped_on_gateway_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A gateway-only host runs no agents → no warm-up process."""
    calls: list = []
    monkeypatch.setattr(_warmup.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    monkeypatch.setattr(_warmup, "logs_dir", lambda: tmp_path)
    _warmup._launch_agent_warmup(frozenset({"gateway"}), tmp_path)
    assert calls == []


def test_launches_on_agent_runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    """An agent-runner host fires `agent.warmup` via the venv interpreter
    (`sys.executable`), detached, cwd=repo."""
    recorded: list = []

    class _FakePopen:
        def __init__(self, argv, **kw) -> None:
            recorded.append((argv, kw))  # pyright: ignore[reportUnknownMemberType]

    monkeypatch.setattr(_warmup.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr("ops.agent_launch.agent_spawn_env_dict", dict)
    monkeypatch.setattr(_warmup, "logs_dir", lambda: tmp_path)

    _warmup._launch_agent_warmup(frozenset({"gateway", "agent-runner"}), tmp_path)

    assert len(recorded) == 1  # pyright: ignore[reportUnknownArgumentType]
    argv, kw = recorded[0]
    assert argv == [sys.executable, "-m", "agent.warmup"]
    assert kw["cwd"] == str(tmp_path)
    assert kw["start_new_session"] is True
    assert "warm-up" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_launch_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """A Popen failure is swallowed (logged), never raised — start must not break."""

    def _boom(*_a, **_kw):
        raise OSError("uv not found")

    monkeypatch.setattr(_warmup.subprocess, "Popen", _boom)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ops.agent_launch.agent_spawn_env_dict", dict)
    monkeypatch.setattr(_warmup, "logs_dir", lambda: tmp_path)

    _warmup._launch_agent_warmup(frozenset({"agent-runner"}), tmp_path)  # must not raise
    assert "skipped" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
