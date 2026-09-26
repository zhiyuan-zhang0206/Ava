"""The agent-ID context note — the identity line of the context head.

Policy: every window states the agent's own id — plus label / machine /
workspace when available — as a system-styled HumanMessage outside the
SystemMessage, so a fork does not carry a stale identity (issue #1320). The
note must resolve the HOSTED turn identity (the agent host hosts many agents'
turns in one process and establishes no process-wide id) — reading the process
slot directly silently dropped this note from every hosted head for two weeks
(task #3939). Each clause is fail-soft: a missing label / machine / workspace
never costs the identity line itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.graph._context_notes import _own_label, agent_id_note
from shared.config import settings
from shared.message_kwargs import NoteTag
from shared.turn_identity import bind_turn_identity


@pytest.fixture(autouse=True)
def _agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The note opts out without an established process identity, like every
    other framework note; give it one so the content is what is under test."""
    monkeypatch.setattr("ava.agent_identity._agent_id", 29)


def _no_label(_agent_id: int) -> str | None:
    return None


def _no_machine() -> str | None:
    return None


@pytest.fixture(autouse=True)
def _no_optional_clauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the label / machine clauses to absent; tests opt them back in.
    The workspace clause stays real but its section gate is off by default here,
    so a test that does not exercise it never touches the filesystem."""
    monkeypatch.setattr("agent.graph._context_notes._own_label", _no_label)
    monkeypatch.setattr("agent.graph._context_notes._machine_clause", _no_machine)
    monkeypatch.setattr(settings.agent, "workspace_in_system_prompt", False)


def _steward_label(_agent_id: int) -> str | None:
    return "memory steward"


def _wsl_machine() -> str | None:
    return "wsl"


def _content() -> str:
    """The note's body with its `[system]` carrier prefix stripped — the
    assertions are about what the note says, not its framing."""
    note = agent_id_note()
    assert note is not None
    assert note.additional_kwargs["ava_note_tag"] == NoteTag.AGENT_ID  # pyright: ignore[reportUnknownMemberType]
    content = str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert content.startswith("[system] ")
    return content[len("[system] ") :]


def test_states_the_id_label_and_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The identity line carries id + label + machine."""
    monkeypatch.setattr("agent.graph._context_notes._own_label", _steward_label)
    monkeypatch.setattr("agent.graph._context_notes._machine_clause", _wsl_machine)

    assert _content().startswith("Your Agent ID is 29 (label: memory steward, machine: wsl).")


def test_id_only_when_no_optional_clauses() -> None:
    """A missing clause is omitted, never rendered empty — and never costs the id."""
    assert _content() == "Your Agent ID is 29."


def test_partial_clauses_render_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.graph._context_notes._own_label", _steward_label)
    assert _content() == "Your Agent ID is 29 (label: memory steward)."

    monkeypatch.setattr("agent.graph._context_notes._own_label", _no_label)
    monkeypatch.setattr("agent.graph._context_notes._machine_clause", _wsl_machine)
    assert _content() == "Your Agent ID is 29 (machine: wsl)."


def test_workspace_clause_carries_the_concrete_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The concrete workspace path lives HERE — the `# Workspace` prompt section
    is id-free for fork safety and points at this note."""
    monkeypatch.setattr(settings.agent, "workspace_in_system_prompt", True)
    ws = tmp_path / "workspaces" / "29"

    def _fake_workspace_dir(_agent_id: int) -> Path:
        return ws

    monkeypatch.setattr("agent.graph._context_notes.workspace_dir", _fake_workspace_dir)
    assert _content().endswith(f" Your workspace is {ws}.")


def test_workspace_clause_respects_the_section_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bench runners that turn the `# Workspace` section off keep their prompts
    free of workspace chatter — the note's path clause honours the same gate."""
    monkeypatch.setattr(settings.agent, "workspace_in_system_prompt", False)

    def _fake_workspace_dir(_agent_id: int) -> Path:
        return tmp_path / "workspaces" / "29"

    monkeypatch.setattr("agent.graph._context_notes.workspace_dir", _fake_workspace_dir)
    assert _content() == "Your Agent ID is 29."


def test_renders_under_a_hosted_turn_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hosted runner pins the identity in a turn contextvar and leaves the
    process slot None; the note must resolve through `ava.agent_identity.agent_id()`
    (task #3939)."""
    monkeypatch.setattr("ava.agent_identity._agent_id", None)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)

    with bind_turn_identity(31):
        note = agent_id_note()

    assert note is not None
    assert "Your Agent ID is 31" in str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_opts_out_without_any_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot renders / dev REPL: no slot, no turn, no env — decline."""
    monkeypatch.setattr("ava.agent_identity._agent_id", None)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    assert agent_id_note() is None


# ── clause readers ──


class _FakeCursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, *args: object, **kwargs: object) -> None:
        return None

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _FakeDB:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self._row = row

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._row)


def test_own_label_normalizes_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A label is free text; the one-line note gets it whitespace-collapsed."""
    monkeypatch.setattr("ava.DB", _FakeDB(("  memory\n  steward ",)))
    assert _own_label(29) == "memory steward"


def test_own_label_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No row / empty label / read failure all degrade to "no label clause" —
    the identity line itself outranks the label."""
    monkeypatch.setattr("ava.DB", _FakeDB(None))
    assert _own_label(29) is None

    monkeypatch.setattr("ava.DB", _FakeDB(("",)))
    assert _own_label(29) is None

    class _Boom:
        def cursor(self) -> object:
            raise RuntimeError("db down")

    monkeypatch.setattr("ava.DB", _Boom())
    assert _own_label(29) is None
