"""The agent-ID context note — the identity line of the context head.

Policy: every window states the agent's own id — plus label / machine /
workspace when available — as a system-styled HumanMessage outside the
SystemMessage, so a fork does not carry a stale identity (issue #1320). The
note reads the explicit host context because many agents share one process.
Missing optional values do not cost the identity line; label-read failures
propagate to the caller.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from psycopg import OperationalError

from agent.graph.prompt.context_notes import _machine_clause, _own_label, agent_id_note
from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import NoteTag
from base.clock import Clock
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.lm.plugin_providers import build_model_catalog
from tests.fixtures.pin_agent import pin_agent, pin_no_identity


@pytest.fixture(autouse=True)
def _agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The note opts out without an established process identity, like every
    other framework note; give it one so the content is what is under test."""
    pin_agent(29)


def _context(agent_id: int | None = 29) -> AvaContext:
    return AvaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True) if agent_id is not None else None,
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )


def _no_label(_ctx: AvaContext, _agent_id: int) -> str | None:
    return None


def _no_machine() -> str | None:
    return None


@pytest.fixture(autouse=True)
def _no_optional_clauses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the label / machine clauses to absent; tests opt them back in.
    The workspace clause stays real but its section gate is off by default here,
    so a test that does not exercise it never touches the filesystem."""
    monkeypatch.setattr("agent.graph.prompt.context_notes._own_label", _no_label)
    monkeypatch.setattr("agent.graph.prompt.context_notes._machine_clause", _no_machine)
    monkeypatch.setattr(settings.agent, "workspace_in_system_prompt", False)


def _steward_label(_ctx: AvaContext, _agent_id: int) -> str | None:
    return "memory steward"


def _wsl_machine() -> str | None:
    return "wsl"


def _content() -> str:
    """The note's body with its `[system]` carrier prefix stripped — the
    assertions are about what the note says, not its framing."""
    note = agent_id_note(_context())
    assert note is not None
    assert note.additional_kwargs["ava_note_tag"] == NoteTag.AGENT_ID  # pyright: ignore[reportUnknownMemberType]
    content = str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    assert content.startswith("[system] ")
    return content[len("[system] ") :]


def test_states_the_id_label_and_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The identity line carries id + label + machine."""
    monkeypatch.setattr("agent.graph.prompt.context_notes._own_label", _steward_label)
    monkeypatch.setattr("agent.graph.prompt.context_notes._machine_clause", _wsl_machine)

    assert _content().startswith("Your Agent ID is 29 (label: memory steward, machine: wsl).")


def test_id_only_when_no_optional_clauses() -> None:
    """A missing clause is omitted, never rendered empty — and never costs the id."""
    assert _content() == "Your Agent ID is 29."


def test_partial_clauses_render_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.graph.prompt.context_notes._own_label", _steward_label)
    assert _content() == "Your Agent ID is 29 (label: memory steward)."

    monkeypatch.setattr("agent.graph.prompt.context_notes._own_label", _no_label)
    monkeypatch.setattr("agent.graph.prompt.context_notes._machine_clause", _wsl_machine)
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

    monkeypatch.setattr("agent.graph.prompt.context_notes.workspace_dir", _fake_workspace_dir)
    assert _content().endswith(f" Your workspace is {ws}.")


def test_workspace_clause_respects_the_section_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bench runners that turn the `# Workspace` section off keep their prompts
    free of workspace chatter — the note's path clause honours the same gate."""
    monkeypatch.setattr(settings.agent, "workspace_in_system_prompt", False)

    def _fake_workspace_dir(_agent_id: int) -> Path:
        return tmp_path / "workspaces" / "29"

    monkeypatch.setattr("agent.graph.prompt.context_notes.workspace_dir", _fake_workspace_dir)
    assert _content() == "Your Agent ID is 29."


def test_renders_under_a_hosted_turn_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit host context wins over unrelated native turn identity."""
    context = _context(31)
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    note = agent_id_note(context)

    assert note is not None
    assert "Your Agent ID is 31" in str(note.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]


def test_opts_out_without_any_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot renders / dev REPL: no slot, no turn, no env — decline."""
    context = _context(None)
    pin_no_identity()
    monkeypatch.delenv("AVA_AGENT_ID", raising=False)
    assert agent_id_note(context) is None


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


def _fake_sql(monkeypatch: pytest.MonkeyPatch, fake: object) -> AvaContext:
    """Supply this note's SQL connection without binding the shared SDK."""
    ctx = _context()
    monkeypatch.setattr(ctx.clients, "_sql", fake)
    return ctx


def test_own_label_normalizes_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A label is free text; the one-line note gets it whitespace-collapsed."""
    ctx = _fake_sql(monkeypatch, _FakeDB(("  memory\n  steward ",)))
    assert _own_label(ctx, 29) == "memory steward"


@pytest.mark.parametrize("row", [None, (None,), ("",)])
def test_own_label_omits_missing_data(
    monkeypatch: pytest.MonkeyPatch, row: tuple[object, ...] | None
) -> None:
    ctx = _fake_sql(monkeypatch, _FakeDB(row))
    assert _own_label(ctx, 29) is None


@pytest.mark.parametrize("error_type", [OperationalError, RuntimeError])
def test_own_label_propagates_cursor_failure(
    monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    failure = error_type("label connection is unavailable")

    class _Boom:
        def cursor(self) -> object:
            raise failure

    ctx = _fake_sql(monkeypatch, _Boom())
    with pytest.raises(error_type) as raised:
        _own_label(ctx, 29)
    assert raised.value is failure


def test_agent_id_note_propagates_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = OperationalError("label query failed")

    class _BrokenCursor(_FakeCursor):
        def execute(self, *args: object, **kwargs: object) -> None:
            raise failure

    class _BrokenDB:
        def cursor(self) -> _BrokenCursor:
            return _BrokenCursor(None)

    ctx = _fake_sql(monkeypatch, _BrokenDB())
    monkeypatch.setattr("agent.graph.prompt.context_notes._own_label", _own_label)
    with pytest.raises(OperationalError) as raised:
        agent_id_note(ctx)
    assert raised.value is failure


def test_unset_machine_name_drops_only_the_machine_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset machine name is an expected condition; any other failure reading it surfaces."""
    from base.cluster import machine

    def _unset() -> str:
        raise machine.MachineNameMissing("unset")

    def _broken() -> str:
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(machine, "machine_name", _unset)
    assert _machine_clause() is None
    monkeypatch.setattr(machine, "machine_name", _broken)
    with pytest.raises(RuntimeError, match="settings unreadable"):
        _machine_clause()
