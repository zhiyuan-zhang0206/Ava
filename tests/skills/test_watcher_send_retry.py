"""Hermetic delivery-retry tests for the skill reference watchers.

Four reference watchers wake the launching agent with a single send at their
trigger point: ``watch_idle.py`` (ava-watcher and ava-goal), ``watch_work.py``
(ava-use-claude-code-and-codex), and ``gather_files.py``
(ava-dynamic-workflow). A gateway / agent restart window (an update wave,
``ava cluster update``) outlasts the SDK's own 3 quick retries; before this the
exception killed the watcher and the wake was lost (2026-09-17, task #3694 —
the same class as #2663's ci_watcher fix). Each template now retries delivery
with doubling gaps and exits 2 when every attempt failed. These tests pin the
retry, the channel routing, and the exhausted-exit contract hermetically: real
imports, a fake ``ava`` that records sends and can refuse the first N of them.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO = Path(__file__).parents[2]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_WATCH_IDLE_PATHS = {
    "ava-watcher": _REPO / "ava_builtins/skills/ava-watcher/reference/watch_idle.py",
    "ava-goal": _REPO / "ava_builtins/skills/ava-goal/reference/watch_idle.py",
}
watch_idle_modules = [
    _load(f"watch_idle_{label.replace('-', '_')}_under_test", path)
    for label, path in _WATCH_IDLE_PATHS.items()
]
watch_work = _load(
    "watch_work_retry_under_test",
    _REPO / "ava_builtins/skills/ava-use-claude-code-and-codex/reference/watch_work.py",
)
gather_files = _load(
    "gather_files_under_test",
    _REPO / "ava_builtins/skills/ava-dynamic-workflow/reference/gather_files.py",
)


class _FakeAva:
    """Records sends; the first `send_failures` attempts raise (a restart window)."""

    def __init__(self, send_failures: int = 0) -> None:
        self.calls = 0
        self.send_failures = send_failures
        self.sent: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        fake = self

        class _Agents:
            @staticmethod
            def send_message(agent_id: int, content: str) -> None:
                fake._attempt("send_message", (agent_id, content), {})

            @staticmethod
            def send_system_note(agent_id: int, content: str, *, tag: str, resurrect: bool) -> None:
                fake._attempt(
                    "send_system_note", (agent_id, content), {"tag": tag, "resurrect": resurrect}
                )

        class _Self:
            AGENT_ID = 42

        self.agents = _Agents()
        self.self = _Self()

    def _attempt(self, channel: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.calls += 1
        if self.calls <= self.send_failures:
            raise RuntimeError("gateway refused the connection")
        self.sent.append((channel, args, kwargs))


def _wire(monkeypatch: pytest.MonkeyPatch, module: ModuleType, send_failures: int = 0) -> _FakeAva:
    """Swap in the fake `ava`; pin the retry budget and zero its wall clock.

    The constants are pinned here so a template whose schedule moves fails
    loudly in this suite rather than silently changing what is asserted below.
    """
    assert module.WAKE_ATTEMPTS == 8
    assert module.WAKE_BACKOFF_S == 10.0
    assert module.WAKE_BACKOFF_MAX_S == 160.0
    fake = _FakeAva(send_failures)
    monkeypatch.setattr(module, "ava", fake)
    monkeypatch.setattr(module, "WAKE_BACKOFF_S", 0.0)
    return fake


@pytest.mark.parametrize("module", watch_idle_modules, ids=list(_WATCH_IDLE_PATHS))
def test_watch_idle_retries_across_a_restart_window(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType
) -> None:
    """Refused sends (a restart window) then a landing one: the idle reminder
    still reaches the launching agent without a manual re-launch."""
    fake = _wire(monkeypatch, module, send_failures=2)
    module._notify(7)

    assert fake.calls == 3
    channel, args, _ = fake.sent[0]
    assert channel == "send_message"
    assert args[0] == 42  # ava.self.AGENT_ID — the launching agent
    assert "target agent 7 idled" in args[1]


@pytest.mark.parametrize("module", watch_idle_modules, ids=list(_WATCH_IDLE_PATHS))
def test_watch_idle_exhausted_delivery_exits_2(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every attempt refused is not a silent nothing: the watcher exits 2 and
    the session log carries the failed attempts."""
    fake = _wire(monkeypatch, module, send_failures=100)
    with pytest.raises(SystemExit) as excinfo:
        module._notify(7)

    assert excinfo.value.code == 2
    assert fake.calls == 8
    assert "wake delivery failed after 8 attempts" in capsys.readouterr().out


def _arm_gather(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, send_failures: int) -> _FakeAva:
    """Point the checkpoint watcher at a tmp handoff dir with both files landed."""
    fake = _wire(monkeypatch, gather_files, send_failures=send_failures)
    monkeypatch.setattr(gather_files, "_dir", tmp_path)
    monkeypatch.setattr(gather_files, "HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr(gather_files, "EXPECTED_FILES", ["a.json", "b.json"])
    monkeypatch.setattr(gather_files, "ORCHESTRATOR_ID", 42)
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.json").write_text("{}")
    return fake


def test_gather_files_retries_then_delivers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The checkpoint fires through a refused window instead of dying there."""
    fake = _arm_gather(monkeypatch, tmp_path, send_failures=2)
    gather_files.watch(0)

    assert fake.calls == 3
    channel, args, _ = fake.sent[0]
    assert channel == "send_message"
    assert args[0] == 42
    assert "checkpoint reached" in args[1] and "(2/2)" in args[1]


def test_gather_files_exhausted_delivery_exits_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _arm_gather(monkeypatch, tmp_path, send_failures=100)
    with pytest.raises(SystemExit) as excinfo:
        gather_files.watch(0)

    assert excinfo.value.code == 2
    assert fake.calls == 8
    assert "wake delivery failed after 8 attempts" in capsys.readouterr().out


def test_watch_work_notify_retries_generic_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generic channel retries; True means the message landed."""
    fake = _wire(monkeypatch, watch_work, send_failures=2)
    assert watch_work._notify(41, "hello", canonical=False) is True

    assert fake.calls == 3
    channel, args, _ = fake.sent[0]
    assert channel == "send_message"
    assert args == (41, "hello")


def test_watch_work_notify_routes_canonical_through_system_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical supervision speaks through the system-note channel."""
    fake = _wire(monkeypatch, watch_work)
    assert watch_work._notify(41, "cleanup done", canonical=True) is True

    channel, args, kwargs = fake.sent[0]
    assert channel == "send_system_note"
    assert args == (41, "cleanup done")
    assert kwargs == {"tag": "task", "resurrect": False}


def test_watch_work_exhausted_delivery_reports_false(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """False (not an exception) when every attempt failed — the in-loop call
    sites keep supervising on it, so the loop sites must opt in to exiting."""
    fake = _wire(monkeypatch, watch_work, send_failures=100)
    assert watch_work._notify(41, "hello", canonical=False) is False

    assert fake.calls == 8
    assert "wake delivery failed after 8 attempts" in capsys.readouterr().out


def test_watch_work_terminal_wake_delivers_through_the_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An actionable STATUS wakes the launching agent even when the first
    sends are refused."""
    fake = _wire(monkeypatch, watch_work, send_failures=2)
    work = tmp_path / "work.md"
    work.write_text("STATUS: DONE\n")

    watch_work.watch(str(work))

    assert fake.calls == 3
    channel, args, _ = fake.sent[0]
    assert channel == "send_message"
    assert "STATUS: DONE" in args[1]


def test_watch_work_terminal_wake_exits_2_on_exhaustion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The generic one-shot path: a wake that never lands exits 2 instead of
    ending as if it had been delivered."""
    _wire(monkeypatch, watch_work, send_failures=100)
    work = tmp_path / "work.md"
    work.write_text("STATUS: DONE\n")

    with pytest.raises(SystemExit) as excinfo:
        watch_work.watch(str(work))

    assert excinfo.value.code == 2
