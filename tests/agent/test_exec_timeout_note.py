"""The execute-code-timeout context note.

Policy: the hard wall-clock bound on `execute_code` is declared once per window,
because the failure it prevents — a call silently killed mid-work — is expensive
to discover from a traceback alone. The note renders the value from
`settings.sandbox.exec_timeout_seconds`, and must resolve the HOSTED turn
identity (the agent host hosts many agents' turns in one process and
establishes no process-wide id — task #3939).
"""

from __future__ import annotations

import pytest

from agent.graph._context_notes import exec_timeout_note
from shared.config import settings
from shared.message_kwargs import NoteTag
from shared.turn_identity import bind_turn_identity


@pytest.fixture(autouse=True)
def _agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The note opts out without an established process identity, like every
    other framework note; give it one so the content is what is under test."""
    monkeypatch.setattr("ava.agent_identity._agent_id", 7)


def test_declares_the_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The value comes from the setting, not a hard-coded constant."""
    monkeypatch.setattr(settings.sandbox, "exec_timeout_seconds", 600.0)
    note = exec_timeout_note()
    assert note is not None
    content = str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "600 seconds (10 minutes)" in content
    assert "it will be killed" in content
    assert note.additional_kwargs["ava_note_tag"] == NoteTag.EXEC_TIMEOUT  # pyright: ignore[reportUnknownMemberType]


def test_opts_out_without_an_agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ava.agent_identity._agent_id", None)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    assert exec_timeout_note() is None


def test_renders_under_a_hosted_turn_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hosted runner pins the identity in a turn contextvar and leaves the
    process slot None; the note must resolve through `ava.agent_identity.agent_id()`
    (task #3939)."""
    monkeypatch.setattr("ava.agent_identity._agent_id", None)
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)

    with bind_turn_identity(29):
        note = exec_timeout_note()

    assert note is not None
    assert "hard wall-clock timeout" in str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
