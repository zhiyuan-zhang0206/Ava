"""The execute-code-timeout context note.

Policy: the hard wall-clock bound on `execute_code` is declared once per window,
because the failure it prevents — a call silently killed mid-work — is expensive
to discover from a traceback alone. The note renders the value from
`settings.sandbox.exec_timeout_seconds`, and must resolve the HOSTED turn
identity carried by the explicit host context.
"""

from __future__ import annotations

import pytest

from agent.graph.prompt.context_notes import exec_timeout_note
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import NoteTag
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from tests.fixtures.pin_agent import pin_agent, pin_no_identity


@pytest.fixture(autouse=True)
def _agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The note opts out without an established process identity, like every
    other framework note; give it one so the content is what is under test."""
    pin_agent(7)


def _context(agent_id: int | None = 7) -> AvaContext:
    return AvaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True) if agent_id is not None else None,
        agent=AgentSlices.resolve(),
    )


def test_declares_the_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The value comes from the setting, not a hard-coded constant."""
    monkeypatch.setattr(settings.sandbox, "exec_timeout_seconds", 600.0)
    note = exec_timeout_note(_context())
    assert note is not None
    content = str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert "600 seconds (10 minutes)" in content
    assert "it will be killed" in content
    assert note.additional_kwargs["ava_note_tag"] == NoteTag.EXEC_TIMEOUT  # pyright: ignore[reportUnknownMemberType]


def test_opts_out_without_an_agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _context(None)
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    assert exec_timeout_note(context) is None


def test_renders_under_a_hosted_turn_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The explicit host context works without binding the shared SDK."""
    context = _context(29)
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    note = exec_timeout_note(context)

    assert note is not None
    assert "hard wall-clock timeout" in str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
