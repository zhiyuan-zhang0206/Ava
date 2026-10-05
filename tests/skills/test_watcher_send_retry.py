"""Hermetic delivery-retry tests for the skill reference watchers, plus the
ava-dynamic-workflow orchestrator scripts' handoff-dir resolution.

Five reference watchers wake the launching agent with a single send at their
trigger point: ``watch_idle.py`` (ava-watcher, ava-goal, and ava-fleet), ``watch_work.py``
(ava-guide.external-agents), and ``gather_files.py``
(ava-dynamic-workflow). A gateway / agent restart window (an update wave,
the fleet update) outlasts the SDK's own 3 quick retries; before this the
exception killed the watcher and the wake was lost (2026-09-17, task #3694 —
the same class as #2663's ci_watcher fix). Each template now retries delivery
with doubling gaps and exits 2 when every attempt failed. These tests pin the
retry, the channel routing, and the exhausted-exit contract hermetically: real
imports, a fake ``ava`` that records sends and can refuse the first N of them.

The handoff-dir tests below (2026-09-28 fix, PR #3550 follow-up P2-1) lock
`codebase_sweep_lite.py`, `deep_research_lite.py` (`ava-dynamic-workflow/scripts/`),
`codebase_sweep_orchestrator.py`, `deep_research_orchestrator.py`, and
`orchestrator_template.py` (`ava-dynamic-workflow/references/`, alongside
`gather_files.py` above) to `base.paths.workspace_dir` instead of a hardcoded
`Path.home() / ".ava/workspaces"` that never read `AVA_HOME` at all. On a non-default home
cluster (a worktree cluster, home `~/.ava-<dir>`) that silently wrote
scratch/handoff state into the WRONG cluster's workspace tree.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from base.paths import workspace_dir
from base.sessions.coding_session_owner import CodingSessionKey, CodingSessionOwner
from base.sessions.coding_session_owner_record import CodingSessionStatus

_REPO = Path(__file__).parents[2]
_DYNAMIC_WORKFLOW_SCRIPTS = (
    _REPO / "ava_builtins" / "skills" / "coordination" / "ava-dynamic-workflow" / "scripts"
)
_DYNAMIC_WORKFLOW_REFERENCES = (
    _REPO / "ava_builtins" / "skills" / "coordination" / "ava-dynamic-workflow" / "references"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_WATCH_IDLE_PATHS = {
    "ava-watcher": _REPO / "ava_builtins/skills/coordination/ava-watcher/scripts/watch_idle.py",
    "ava-goal": _REPO / "ava_builtins/skills/coordination/ava-goal/scripts/watch_idle.py",
    "ava-fleet": _REPO / "ava_builtins/plugins/ava_fleet/skills/ava-fleet/reference/watch_idle.py",
}
watch_idle_modules = [
    _load(f"watch_idle_{label.replace('-', '_')}_under_test", path)
    for label, path in _WATCH_IDLE_PATHS.items()
]
watch_work = _load(
    "watch_work_retry_under_test",
    _REPO / "ava_builtins/skills/platform/ava-guide/external-agents/scripts/watch_work.py",
)
gather_files = _load(
    "gather_files_under_test",
    _DYNAMIC_WORKFLOW_REFERENCES / "gather_files.py",
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


def test_watch_idle_bodies_match_after_module_docstring() -> None:
    """The three copyable watchers share one byte-identical executable body."""
    bodies: dict[str, bytes] = {}
    for label, path in _WATCH_IDLE_PATHS.items():
        source = path.read_bytes()
        first = ast.parse(source).body[0]
        assert isinstance(first, ast.Expr)
        assert isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
        assert first.lineno == 1 and first.col_offset == 0
        assert first.end_lineno is not None and first.end_col_offset is not None
        lines = source.splitlines(keepends=True)
        offset = sum(map(len, lines[: first.end_lineno - 1])) + first.end_col_offset
        bodies[label] = source[offset:]

    reference = bodies["ava-watcher"]
    for label, body in bodies.items():
        assert body == reference, f"{label} watch_idle body differs from ava-watcher"


def _rewrite_as_done_after_first_sleep(
    monkeypatch: pytest.MonkeyPatch, work: Path, fake: _FakeAva
) -> None:
    """Advance one generic poll, then make an actionable update observable."""
    slept = False

    def _sleep(_seconds: float) -> None:
        nonlocal slept
        if slept:
            return
        slept = True
        assert fake.calls == 0
        work.write_text("STATUS: DONE\n")
        mtime = work.stat().st_mtime + 1
        os.utime(work, (mtime, mtime))

    monkeypatch.setattr(watch_work.time, "sleep", _sleep)


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
    """A new actionable status wakes through a refused delivery window."""
    fake = _wire(monkeypatch, watch_work, send_failures=2)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")
    _rewrite_as_done_after_first_sleep(monkeypatch, work, fake)

    watch_work.watch(str(work))

    assert fake.calls == 3
    channel, args, _ = fake.sent[0]
    assert channel == "send_message"
    assert "STATUS: DONE" in args[1]


def test_watch_work_baselines_actionable_status_until_its_mtime_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A re-armed stale DONE waits for a subsequent file rewrite to wake."""
    fake = _wire(monkeypatch, watch_work)
    work = tmp_path / "work.md"
    work.write_text("STATUS: DONE\n")
    _rewrite_as_done_after_first_sleep(monkeypatch, work, fake)

    watch_work.watch(str(work))

    assert fake.calls == 1
    assert "STATUS: DONE" in fake.sent[0][1][1]


def test_watch_work_heartbeats_an_unchanged_baselined_actionable_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale actionable status wakes once through the heartbeat branch."""
    fake = _wire(monkeypatch, watch_work)
    monkeypatch.setattr(watch_work, "HEARTBEAT_SECONDS", 0)
    times = iter((0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0))
    monkeypatch.setattr(watch_work.time, "monotonic", lambda: next(times))
    work = tmp_path / "work.md"
    work.write_text("STATUS: DONE\n")

    watch_work.watch(str(work))

    assert fake.calls == 1
    assert "unchanged since arming" in fake.sent[0][1][1]


def test_watch_work_canonical_need_input_wakes_on_its_first_eligible_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Canonical supervision must not baseline an already-current NEED_INPUT."""
    work = tmp_path / "work.md"
    work.write_text("STATUS: NEED_INPUT\n")
    key = watch_work.coding_session_owner.canonical_key(tmp_path, tool="codex", cluster=tmp_path)
    owner = CodingSessionOwner(
        key=key,
        generation="generation",
        status=CodingSessionStatus.ACTIVE,
        created_at=dt.datetime.fromtimestamp(work.stat().st_mtime - 1, dt.UTC),
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
    )

    def _read(_key: CodingSessionKey, generation: str) -> CodingSessionOwner:
        assert _key == key
        assert generation == owner.generation
        return owner

    def _owner_terminated(_agent_id: int) -> bool:
        return False

    def _session_crashed(_owner: CodingSessionOwner) -> bool:
        return False

    monkeypatch.setattr(watch_work.coding_session_owner, "read", _read)
    monkeypatch.setattr(watch_work, "_owner_terminated", _owner_terminated)
    monkeypatch.setattr(watch_work, "_session_crashed", _session_crashed)
    messages: list[str] = []

    class CanonicalWakeError(RuntimeError):
        pass

    def _notify(_agent_id: int, message: str, *, canonical: bool) -> bool:
        assert canonical
        messages.append(message)
        raise CanonicalWakeError

    def _unexpected_sleep(_seconds: float) -> None:
        raise AssertionError("canonical NEED_INPUT did not wake on its first eligible poll")

    monkeypatch.setattr(watch_work, "_notify", _notify)
    monkeypatch.setattr(watch_work.time, "sleep", _unexpected_sleep)

    with pytest.raises(CanonicalWakeError):
        watch_work.watch(
            str(work),
            cluster=tmp_path,
            workspace=tmp_path,
            generation="generation",
            owner_agent_id=41,
        )

    assert messages == [
        f"coding agent reported STATUS: NEED_INPUT in {work} -- read the file and reply"
    ]


def test_watch_work_terminal_wake_exits_2_on_exhaustion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The generic one-shot path: a wake that never lands exits 2 instead of
    ending as if it had been delivered."""
    fake = _wire(monkeypatch, watch_work, send_failures=100)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")
    _rewrite_as_done_after_first_sleep(monkeypatch, work, fake)

    with pytest.raises(SystemExit) as excinfo:
        watch_work.watch(str(work))

    assert excinfo.value.code == 2


# --------------------------------------------------------------------------- #
# ava-dynamic-workflow orchestrator scripts' handoff dir (P2-1)
# --------------------------------------------------------------------------- #


def _exec_prefix_through(path: Path, target: str, namespace: dict[str, Any]) -> None:
    """Exec `path`'s top-level statements up to and including its first
    `target = ...` assignment, then stop.

    The three full orchestrators (`codebase_sweep_orchestrator.py`,
    `deep_research_orchestrator.py`, `orchestrator_template.py`) run their
    fork/spawn logic as more top-level statements immediately after the
    handoff-dir assignment — they are meant to be pasted whole into
    `execute_code`. Execing the whole file would spawn real agents; stopping
    right after `target` is assigned is enough to prove how the handoff dir
    resolves without touching any of that.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prefix: list[ast.stmt] = []
    for node in tree.body:
        prefix.append(node)
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == target
        ):
            break
    else:
        raise AssertionError(f"no top-level assignment to {target!r} found in {path}")
    module = ast.Module(body=prefix, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)


@pytest.fixture
def _fake_ava_module(monkeypatch: pytest.MonkeyPatch) -> _FakeAva:
    """`_FakeAva().self.AGENT_ID` (42) doubles as the top-level `import ava`
    these reference scripts do — no need for a second fake."""
    fake = _FakeAva()
    monkeypatch.setitem(sys.modules, "ava", fake)
    return fake


@pytest.mark.parametrize(
    ("filename", "target", "suffix"),
    [
        ("codebase_sweep_lite.py", "HD", "codebase_sweep_lite"),
        ("deep_research_lite.py", "HD", "deep_research_lite"),
    ],
)
def test_lite_script_handoff_dir_uses_workspace_dir(
    _fake_ava_module: _FakeAva, filename: str, target: str, suffix: str
) -> None:
    """Importing a lite script only defines functions at module top level
    (unlike the full orchestrators below), so a full exec is safe here."""
    namespace: dict[str, Any] = {"__name__": f"{filename}_under_test"}
    exec(
        compile(
            (_DYNAMIC_WORKFLOW_SCRIPTS / filename).read_text(encoding="utf-8"), filename, "exec"
        ),
        namespace,
    )
    assert namespace[target] == workspace_dir(_fake_ava_module.self.AGENT_ID) / suffix


@pytest.mark.parametrize(
    ("filename", "target", "task_var"),
    [
        ("codebase_sweep_orchestrator.py", "HANDOFF", "TASK"),
        ("deep_research_orchestrator.py", "HANDOFF", "TASK"),
    ],
)
def test_full_orchestrator_handoff_dir_uses_workspace_dir(
    _fake_ava_module: _FakeAva, filename: str, target: str, task_var: str
) -> None:
    namespace: dict[str, Any] = {"__name__": f"{filename}_under_test"}
    _exec_prefix_through(_DYNAMIC_WORKFLOW_REFERENCES / filename, target, namespace)
    assert namespace[target] == workspace_dir(_fake_ava_module.self.AGENT_ID) / namespace[task_var]


def test_orchestrator_template_handoff_dir_uses_workspace_dir(_fake_ava_module: _FakeAva) -> None:
    namespace: dict[str, Any] = {"__name__": "orchestrator_template_under_test"}
    _exec_prefix_through(
        _DYNAMIC_WORKFLOW_REFERENCES / "orchestrator_template.py", "handoff", namespace
    )
    assert namespace["handoff"] == workspace_dir(_fake_ava_module.self.AGENT_ID) / "task_handoff"
