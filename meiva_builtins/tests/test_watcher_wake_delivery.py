"""Hermetic wake-delivery tests for the skill reference watchers, plus the
meiva-dynamic-workflow orchestrator scripts' handoff-dir resolution.

Five reference watchers wake the launching agent with a single send at their
trigger point: ``watch_idle.py`` (long-running-agent, meiva-goal, and meiva-fleet), ``watch_work.py``
(meiva-guide.external-agents), and ``gather_files.py``
(meiva-dynamic-workflow). The send is made once; the SDK's own bounded same-key
retries are the only retry. A send that still fails is not silent: the one-shot
watchers log it and exit 2 (the exit notice reaches the launcher), and the
``watch_work.py`` supervision loop reports False and tries again at its next
trigger. These tests pin the single send, the channel routing, and the
exit-2 contract hermetically: real imports, a fake ``meiva`` that records sends
and can refuse the first N of them.

The handoff-dir tests below (2026-09-28 fix, PR #3550 follow-up P2-1) lock
`codebase_sweep_lite.py`, `deep_research_lite.py` (`meiva-dynamic-workflow/scripts/`),
`codebase_sweep_orchestrator.py`, `deep_research_orchestrator.py`, and
`orchestrator_template.py` (`meiva-dynamic-workflow/references/`, alongside
`gather_files.py` above) to `base.paths.workspace_dir` instead of a hardcoded
`Path.home() / ".meiva/workspaces"` that never read `MEIVA_HOME` at all. On a non-default home
cluster (a worktree cluster, home `~/.meiva-<dir>`) that silently wrote
scratch/handoff state into the WRONG cluster's workspace tree.
"""

from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import meiva
from base.paths import workspace_dir
from base.sessions.coding_session_owner import CodingSessionKey, CodingSessionOwner
from base.sessions.coding_session_owner_record import CodingSessionStatus

_REPO = Path(__file__).parents[2]
_DYNAMIC_WORKFLOW_SCRIPTS = (
    _REPO / "meiva_builtins" / "skills" / "coordination" / "meiva-dynamic-workflow" / "scripts"
)
_DYNAMIC_WORKFLOW_REFERENCES = (
    _REPO / "meiva_builtins" / "skills" / "coordination" / "meiva-dynamic-workflow" / "references"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_WATCH_IDLE_PATHS = {
    "long-running-agent": _REPO
    / "meiva_builtins/skills/coordination/meiva-being-a-long-running-agent/scripts/watch_idle.py",
    "meiva-goal": _REPO / "meiva_builtins/skills/coordination/meiva-goal/scripts/watch_idle.py",
    "meiva-fleet": _REPO
    / "meiva_builtins/plugins/meiva_fleet/skills/meiva-fleet/reference/watch_idle.py",
}
watch_idle_modules = [
    _load(f"watch_idle_{label.replace('-', '_')}_under_test", path)
    for label, path in _WATCH_IDLE_PATHS.items()
]
watch_work = _load(
    "watch_work_retry_under_test",
    _REPO / "meiva_builtins/skills/platform/meiva-guide/external-agents/scripts/watch_work.py",
)
gather_files = _load(
    "gather_files_under_test",
    _DYNAMIC_WORKFLOW_REFERENCES / "gather_files.py",
)


class _FakeMeiva:
    """Records sends; the first `send_failures` attempts raise (a restart window)."""

    def __init__(self, send_failures: int = 0) -> None:
        self.calls = 0
        self.send_failures = send_failures
        self.sent: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.note_keys: list[str] = []
        fake = self

        class _Agents:
            @staticmethod
            def send_message(agent_id: int, content: str) -> None:
                fake._attempt("send_message", (agent_id, content), {})

            @staticmethod
            def send_system_note(
                agent_id: int, content: str, *, tag: str, resurrect: bool, idempotency_key: str
            ) -> None:
                fake._attempt(
                    "send_system_note",
                    (agent_id, content),
                    {"tag": tag, "resurrect": resurrect, "idempotency_key": idempotency_key},
                )

        class _Self:
            AGENT_ID = 42

        self.agents = _Agents()
        self.self = _Self()

    def _attempt(self, channel: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        if channel == "send_system_note":
            self.note_keys.append(kwargs["idempotency_key"])
        self.calls += 1
        if self.calls <= self.send_failures:
            raise RuntimeError("gateway refused the connection")
        self.sent.append((channel, args, kwargs))


def _wire(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, send_failures: int = 0
) -> _FakeMeiva:
    """Swap in the fake `meiva`."""
    fake = _FakeMeiva(send_failures)
    monkeypatch.setattr(module, "meiva", fake)
    return fake


def _forbid_sleep(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> None:
    """A failed wake must not wait out a retry window."""

    def _sleep(seconds: float) -> None:
        raise AssertionError(f"slept {seconds}s after a failed wake")

    monkeypatch.setattr(module.time, "sleep", _sleep)


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

    reference = bodies["long-running-agent"]
    for label, body in bodies.items():
        assert body == reference, f"{label} watch_idle body differs from long-running-agent"


def _rewrite_as_done_after_first_sleep(
    monkeypatch: pytest.MonkeyPatch, work: Path, fake: _FakeMeiva
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
def test_watch_idle_sends_the_reminder_once(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType
) -> None:
    """The idle reminder reaches the launching agent with one send."""
    fake = _wire(monkeypatch, module)
    module._notify(7)

    assert fake.calls == 1
    channel, args, _ = fake.sent[0]
    assert channel == "send_message"
    assert args[0] == 42  # meiva.self.AGENT_ID — the launching agent
    assert "target agent 7 idled" in args[1]


@pytest.mark.parametrize("module", watch_idle_modules, ids=list(_WATCH_IDLE_PATHS))
def test_watch_idle_failed_send_exits_2_without_a_retry_window(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused send is not a silent nothing and not a ten-minute retry loop:
    the watcher logs it and exits 2 after the single attempt."""
    fake = _wire(monkeypatch, module, send_failures=100)
    _forbid_sleep(monkeypatch, module)
    with pytest.raises(SystemExit) as excinfo:
        module._notify(7)

    assert excinfo.value.code == 2
    assert fake.calls == 1
    out = capsys.readouterr().out
    assert "wake delivery failed" in out
    assert "gateway refused the connection" in out


def _arm_gather(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, send_failures: int) -> _FakeMeiva:
    """Point the checkpoint watcher at a tmp handoff dir with both files landed."""
    fake = _wire(monkeypatch, gather_files, send_failures=send_failures)
    monkeypatch.setattr(gather_files, "_dir", tmp_path)
    monkeypatch.setattr(gather_files, "HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr(gather_files, "EXPECTED_FILES", ["a.json", "b.json"])
    monkeypatch.setattr(gather_files, "ORCHESTRATOR_ID", 42)
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.json").write_text("{}")
    return fake


def test_gather_files_sends_the_checkpoint_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = _arm_gather(monkeypatch, tmp_path, send_failures=0)
    gather_files.watch(0)

    assert fake.calls == 1
    channel, args, _ = fake.sent[0]
    assert channel == "send_message"
    assert args[0] == 42
    assert "checkpoint reached" in args[1] and "(2/2)" in args[1]


def test_gather_files_failed_send_exits_2_without_a_retry_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _arm_gather(monkeypatch, tmp_path, send_failures=100)
    _forbid_sleep(monkeypatch, gather_files)
    with pytest.raises(SystemExit) as excinfo:
        gather_files.watch(0)

    assert excinfo.value.code == 2
    assert fake.calls == 1
    out = capsys.readouterr().out
    assert "wake delivery failed" in out
    assert "gateway refused the connection" in out


def test_watch_work_notify_sends_the_generic_wake_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The generic channel sends once; True means the message landed."""
    fake = _wire(monkeypatch, watch_work)
    assert watch_work._notify(41, "hello", canonical=False) is True

    assert fake.calls == 1
    channel, args, _ = fake.sent[0]
    assert channel == "send_message"
    assert args == (41, "hello")


def test_watch_work_notify_routes_canonical_through_system_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical supervision speaks through the system-note channel."""
    fake = _wire(monkeypatch, watch_work)
    assert watch_work._notify(41, "cleanup done", canonical=True) is True

    assert fake.calls == 1
    assert len(fake.note_keys) == 1
    channel, args, kwargs = fake.sent[0]
    assert channel == "send_system_note"
    assert args == (41, "cleanup done")
    assert kwargs["tag"] == "task"
    assert kwargs["resurrect"] is False
    assert isinstance(kwargs["idempotency_key"], str)


def test_watch_work_failed_send_reports_false_without_a_retry_window(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """False (not an exception) after the single attempt: the in-loop call
    sites keep supervising and send again at their next trigger, while the
    one-shot sites opt in to exiting."""
    fake = _wire(monkeypatch, watch_work, send_failures=100)
    _forbid_sleep(monkeypatch, watch_work)
    assert watch_work._notify(41, "hello", canonical=False) is False

    assert fake.calls == 1
    assert "wake delivery failed" in capsys.readouterr().out


def test_watch_work_terminal_wake_delivers_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A new actionable status wakes with a single send."""
    fake = _wire(monkeypatch, watch_work)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")
    _rewrite_as_done_after_first_sleep(monkeypatch, work, fake)

    watch_work.watch(str(work))

    assert fake.calls == 1
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
    monkeypatch.setattr(
        watch_work,
        "time",
        SimpleNamespace(monotonic=lambda: next(times), sleep=watch_work.time.sleep),
    )
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
    """The generic one-shot path: a wake that fails exits 2 instead of ending
    as if it had been delivered."""
    fake = _wire(monkeypatch, watch_work, send_failures=100)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")
    _rewrite_as_done_after_first_sleep(monkeypatch, work, fake)

    with pytest.raises(SystemExit) as excinfo:
        watch_work.watch(str(work))

    assert excinfo.value.code == 2
    assert fake.calls == 1


# --------------------------------------------------------------------------- #
# meiva-dynamic-workflow orchestrator scripts' handoff dir (P2-1)
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
def _fake_meiva_module(monkeypatch: pytest.MonkeyPatch) -> _FakeMeiva:
    """The real `meiva` module with `meiva.self` answering as `_FakeMeiva().self` (agent 42): the
    reference scripts do a top-level `import meiva` and read only `meiva.self.AGENT_ID`."""
    fake = _FakeMeiva()
    monkeypatch.setattr(meiva, "self", fake.self, raising=False)
    return fake


@pytest.mark.parametrize(
    ("filename", "target", "suffix"),
    [
        ("codebase_sweep_lite.py", "HD", "codebase_sweep_lite"),
        ("deep_research_lite.py", "HD", "deep_research_lite"),
    ],
)
def test_lite_script_handoff_dir_uses_workspace_dir(
    _fake_meiva_module: _FakeMeiva, filename: str, target: str, suffix: str
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
    assert namespace[target] == workspace_dir(_fake_meiva_module.self.AGENT_ID) / suffix


@pytest.mark.parametrize(
    ("filename", "target", "task_var"),
    [
        ("codebase_sweep_orchestrator.py", "HANDOFF", "TASK"),
        ("deep_research_orchestrator.py", "HANDOFF", "TASK"),
    ],
)
def test_full_orchestrator_handoff_dir_uses_workspace_dir(
    _fake_meiva_module: _FakeMeiva, filename: str, target: str, task_var: str
) -> None:
    namespace: dict[str, Any] = {"__name__": f"{filename}_under_test"}
    _exec_prefix_through(_DYNAMIC_WORKFLOW_REFERENCES / filename, target, namespace)
    assert (
        namespace[target] == workspace_dir(_fake_meiva_module.self.AGENT_ID) / namespace[task_var]
    )


def test_orchestrator_template_handoff_dir_uses_workspace_dir(
    _fake_meiva_module: _FakeMeiva,
) -> None:
    namespace: dict[str, Any] = {"__name__": "orchestrator_template_under_test"}
    _exec_prefix_through(
        _DYNAMIC_WORKFLOW_REFERENCES / "orchestrator_template.py", "handoff", namespace
    )
    assert namespace["handoff"] == workspace_dir(_fake_meiva_module.self.AGENT_ID) / "task_handoff"
