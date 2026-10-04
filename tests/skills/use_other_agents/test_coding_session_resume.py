"""Coding sessions stay resumable: each launch names its tool session, and --resume reopens it.

A cluster stop (or a host reboot, a crash, a TTL expiry) closes the persistent
shell a Claude Code or Codex session runs in; the tool's own conversation store
keeps the history. These contracts cover the launch printing the session id,
the ``--resume`` path reopening exactly that session, and the resumed session
being told it was interrupted.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

from ava.shell.coding_tools import _claude_checks, _common, claude, codex
from base.sessions import coding_session_owner

_SKILL_DIR = (
    Path(__file__).parents[3] / "ava_builtins" / "skills" / "coordination" / "ava-use-other-agents"
)
_SESSION = "01a0e1ac-adc7-7d33-bd13-8ce2c6a686c5"
_OTHER = "01a0e1eb-472f-7832-81f6-77c7d2e47d75"
_STATUS_CARD = f"│  Directory:  /ws\n│  Session:                     {_SESSION}   │\n"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"resume_{name}", _SKILL_DIR / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class _FakeShell:
    """A typed stand-in for ``ava.shell.sessions`` that records what was sent."""

    screen: str = ""
    screen_after_enter: str | None = None
    sent: list[str] = field(default_factory=list[str])
    keys: list[str] = field(default_factory=list[str])

    def send(self, _sid: int, text: str) -> None:
        self.sent.append(text)

    def send_keys(self, _sid: int, *keys: str) -> None:
        self.keys.extend(keys)
        if self.screen_after_enter is not None:
            self.screen = self.screen_after_enter

    def capture(self, _sid: int, **_kwargs: object) -> str:
        return self.screen

    def install(self, monkeypatch: pytest.MonkeyPatch) -> _FakeShell:
        monkeypatch.setattr(codex.ava.shell.sessions, "send", self.send)
        monkeypatch.setattr(codex.ava.shell.sessions, "send_keys", self.send_keys)
        monkeypatch.setattr(codex.ava.shell.sessions, "capture", self.capture)
        return self


def _no_wait(_seconds: float) -> None:
    return None


def _nothing(_sid: int) -> None:
    return None


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex.time, "sleep", _no_wait)


# --- Codex: the session id comes from its own /status card ---------------------


def test_codex_session_id_is_read_from_the_status_card(
    monkeypatch: pytest.MonkeyPatch, _no_sleep: None
) -> None:
    shell = _FakeShell(screen=_STATUS_CARD).install(monkeypatch)

    assert codex._read_session_id(7) == _SESSION
    assert shell.sent == ["/status"]


def test_a_parked_status_gets_one_enter(monkeypatch: pytest.MonkeyPatch, _no_sleep: None) -> None:
    shell = _FakeShell(screen="› /status", screen_after_enter=_STATUS_CARD).install(monkeypatch)

    assert codex._read_session_id(7, timeout=0.02) == _SESSION
    assert shell.keys == ["Enter"]


def test_no_session_id_fails_the_launch(monkeypatch: pytest.MonkeyPatch, _no_sleep: None) -> None:
    _FakeShell(screen="› /status").install(monkeypatch)

    with pytest.raises(RuntimeError, match="could not read the Codex session id"):
        codex._read_session_id(7, timeout=0.02)


# --- Codex: --resume reopens exactly the recorded session ----------------------


def _request(workspace: Path, resume: str | None) -> codex._LaunchRequest:
    return codex._LaunchRequest(
        workspace=workspace,
        tasks_file=workspace / "tasks.md",
        work_file=workspace / "work.md",
        ttl_seconds=3600,
        caller_instance=None,
        takeover_name=None,
        takeover_brief="",
        skill_dir=_SKILL_DIR,
        resume=resume,
    )


def _owner(tmp_path: Path) -> coding_session_owner.CodingSessionOwner:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    key = coding_session_owner.canonical_key(workspace, tool="codex", cluster=tmp_path / "c")
    return coding_session_owner.CodingSessionOwner(key=key, status="launching")


@pytest.mark.parametrize("opened", [_SESSION, _OTHER])
def test_a_resumed_worker_reopens_its_session_and_is_told_it_was_interrupted(
    opened: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _owner(tmp_path)
    workspace = Path(owner.key.workspace)
    sent = _FakeShell().install(monkeypatch).sent

    def _opened(_sid: int) -> str:
        return opened

    monkeypatch.setattr(codex, "_wait_for_ready", _nothing)
    monkeypatch.setattr(codex, "_read_session_id", _opened)
    monkeypatch.setattr(codex, "_verify_submitted", _nothing)

    request = _request(workspace, _SESSION)
    if opened != _SESSION:
        with pytest.raises(RuntimeError, match=f"not the requested {_SESSION}"):
            codex._start_codex(7, owner.key, owner, "g", 41, request)
        assert len(sent) == 1  # the TUI line only: nothing reaches the wrong session
        return
    assert codex._start_codex(7, owner.key, owner, "g", 41, request) == (None, _SESSION)
    assert f"exec codex resume {_SESSION} --dangerously-bypass" in sent[0]
    assert sent[1].startswith("This session was interrupted")
    assert "continue from where you stopped" in sent[1]


# --- Claude: the id is chosen up front, --resume reopens it --------------------


def test_claude_pins_or_reopens_the_session_id(tmp_path: Path) -> None:
    fresh = claude._claude_command(tmp_path, claude_session=_SESSION)
    resumed = claude._claude_command(tmp_path, claude_session=_SESSION, resume=True)

    assert fresh.endswith(f"--dangerously-skip-permissions --session-id {_SESSION} || exit $?")
    assert resumed.endswith(f"--dangerously-skip-permissions --resume {_SESSION} || exit $?")


@pytest.mark.parametrize("resume", [None, _SESSION])
def test_claude_launch_names_its_session(
    resume: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, bool]] = []

    def _supervised(*args: object, resume: bool) -> int:
        seen.append((str(args[-1]), resume))
        return 0

    def _no_first_run() -> None:
        return None

    monkeypatch.setattr(claude, "_preset_claude_first_run", _no_first_run)
    monkeypatch.setattr(claude, "_run_supervised_launch", _supervised)

    claude.launch(
        tmp_path, tmp_path / "t.md", tmp_path / "w.md", 60, skill_dir=_SKILL_DIR, resume=resume
    )

    ((session, resumed),) = seen
    assert resumed is (resume is not None)
    if resume is not None:
        assert session == resume
    else:  # a fresh launch mints a new UUID
        assert _common.session_uuid(session) == session


def test_a_resumed_transcript_needs_one_more_bootstrap_to_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _home() -> Path:
        return tmp_path

    monkeypatch.setattr(Path, "home", _home)
    transcript = tmp_path / ".claude" / "projects" / "-ws" / f"{_SESSION}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"text": "You will take over Ava agent 41"}\n', encoding="utf-8")

    baseline = _claude_checks._bootstrap_count(_SESSION)
    assert baseline == 1
    assert _claude_checks._bootstrap_count(_OTHER) == 0

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write('{"text": "You will take over Ava agent 41 again"}\n')
    assert _claude_checks._bootstrap_count(_SESSION) > baseline


_RESUMED_PANEL = (
    "\u23fa takeover active\n"
    "\u273b Crunched for 59s\n"
    "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "\u276f \n"
    "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle) \u00b7 \u2190 for agents\n"
)


def test_a_resumed_panel_is_ready_without_the_welcome_banner() -> None:
    """A resumed session opens on its history; a shell prompt is never ready."""
    shell = "host:cl-b user$ exec claude --dangerously-skip-permissions --resume x\n"

    assert not _claude_checks._claude_ui_ready(_RESUMED_PANEL)
    assert _claude_checks._claude_ui_ready(_RESUMED_PANEL, resumed=True)
    assert not _claude_checks._claude_ui_ready(shell, resumed=True)


@pytest.mark.parametrize("pasted", [False, True])
def test_a_pasted_bootstrap_is_followed_by_the_operators_own_words(
    pasted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code will not act on instructions that arrive only inside a paste."""
    sent = _FakeShell().install(monkeypatch).sent
    message = "take over Ava agent 41. " * (80 if pasted else 1)

    _claude_checks._send_bootstrap(7, message)

    if pasted:
        assert sent == [f"\x1b[200~{message}\x1b[201~{_claude_checks._OWN_WORDS}"]
    else:
        assert sent == [message]


# --- both: the message, the flag ------------------------------------------------


def test_a_fresh_worker_message_does_not_mention_an_interruption(tmp_path: Path) -> None:
    message = _common.worker_bootstrap(
        tmp_path / "c.md", tmp_path, tmp_path / "t.md", tmp_path / "w.md"
    )
    assert "interrupted" not in message
    assert message.endswith("Now read the task file and start working.")


@pytest.mark.parametrize("script", ["spawn_claude", "spawn_codex"])
@pytest.mark.parametrize(
    "extra", [["--resume", "not-a-session"], ["--resume", _SESSION, "--status"]]
)
def test_resume_takes_a_session_uuid_and_excludes_status(
    script: str, extra: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load(script)
    monkeypatch.setattr(sys, "argv", [f"{script}.py", str(tmp_path), *extra])

    with pytest.raises(SystemExit) as excinfo:
        module.main()
    assert excinfo.value.code == 2
