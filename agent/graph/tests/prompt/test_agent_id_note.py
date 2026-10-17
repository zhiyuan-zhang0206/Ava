from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from psycopg import OperationalError

from agent.graph.prompt import context_notes
from agent.graph.prompt.context_notes import agent_id_note, machine_clause, own_label
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import NoteTag
from base.clock import Clock
from base.config import settings
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog
from meiva.tests.fixtures.pin_agent import pin_agent, pin_no_identity


@pytest.fixture(autouse=True)
def _agent_identity(monkeypatch: pytest.MonkeyPatch) -> None:

    pin_agent(29)


def _context(agent_id: int | None = 29) -> MeivaContext:
    return MeivaContext(
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True) if agent_id is not None else None,
        agent=AgentSlices.resolve(
            default_reader=lambda domain, field: getattr(getattr(settings, domain), field)
        ),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )


def _no_label(_ctx: MeivaContext, _agent_id: int) -> str | None:
    return None


def _no_machine(_ctx: MeivaContext) -> str | None:
    return None


@pytest.fixture(autouse=True)
def _no_optional_clauses(monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.setattr(context_notes, "own_label", _no_label)
    monkeypatch.setattr(context_notes, "machine_clause", _no_machine)
    monkeypatch.setattr(settings.agent, "workspace_in_system_prompt", False)


def _steward_label(_ctx: MeivaContext, _agent_id: int) -> str | None:
    return "memory steward"


def _runner_machine(_ctx: MeivaContext) -> str | None:
    return "runner-a"


def _content() -> str:

    note = agent_id_note(_context())
    assert note is not None
    assert note.additional_kwargs["meiva_note_tag"] == NoteTag.AGENT_ID
    content = str(note.content)
    assert content.startswith("[system] ")
    return content[len("[system] ") :]


def test_states_the_id_label_and_machine(monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.setattr(context_notes, "own_label", _steward_label)
    monkeypatch.setattr(context_notes, "machine_clause", _runner_machine)

    assert _content().startswith("Your Agent ID is 29 (label: memory steward, machine: runner-a).")


def test_id_only_when_no_optional_clauses() -> None:

    assert _content() == "Your Agent ID is 29."


def test_partial_clauses_render_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_notes, "own_label", _steward_label)
    assert _content() == "Your Agent ID is 29 (label: memory steward)."

    monkeypatch.setattr(context_notes, "own_label", _no_label)
    monkeypatch.setattr(context_notes, "machine_clause", _runner_machine)
    assert _content() == "Your Agent ID is 29 (machine: runner-a)."


def test_workspace_clause_carries_the_concrete_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:

    monkeypatch.setattr(settings.agent, "workspace_in_system_prompt", True)
    ws = tmp_path / "workspaces" / "29"

    def _fake_workspace_dir(_agent_id: int) -> Path:
        return ws

    monkeypatch.setattr(context_notes, "workspace_dir", _fake_workspace_dir)
    assert _content().endswith(f" Your workspace is {ws}.")
    ws.mkdir(parents=True)
    document = ws / "to_user.md"
    document.write_text("<p>Waiting on a decision</p>")
    assert document.read_text() in _content()
    document.write_text("<p>The decision has arrived</p>")
    assert document.read_text() in _content()
    assert "Waiting on a decision" not in _content()


def test_workspace_clause_respects_the_section_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:

    monkeypatch.setattr(settings.agent, "workspace_in_system_prompt", False)

    def _fake_workspace_dir(_agent_id: int) -> Path:
        return tmp_path / "workspaces" / "29"

    monkeypatch.setattr(context_notes, "workspace_dir", _fake_workspace_dir)
    assert _content() == "Your Agent ID is 29."


def test_renders_under_a_hosted_turn_identity(monkeypatch: pytest.MonkeyPatch) -> None:

    context = _context(31)
    pin_no_identity()
    monkeypatch.delenv("MEIVA_AGENT_ID", raising=False)
    note = agent_id_note(context)

    assert note is not None
    assert "Your Agent ID is 31" in str(note.content)


def test_opts_out_without_any_identity(monkeypatch: pytest.MonkeyPatch) -> None:

    context = _context(None)
    pin_no_identity()
    monkeypatch.delenv("MEIVA_AGENT_ID", raising=False)
    assert agent_id_note(context) is None


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


def _fake_sql(monkeypatch: pytest.MonkeyPatch, fake: object) -> MeivaContext:

    ctx = _context()
    monkeypatch.setattr(ctx.clients, "_sql", fake)
    return ctx


def test_own_label_normalizes_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:

    ctx = _fake_sql(monkeypatch, _FakeDB(("  memory\n  steward ",)))
    assert own_label(ctx, 29) == "memory steward"


@pytest.mark.parametrize("row", [None, (None,), ("",)])
def test_own_label_omits_missing_data(
    monkeypatch: pytest.MonkeyPatch, row: tuple[object, ...] | None
) -> None:
    ctx = _fake_sql(monkeypatch, _FakeDB(row))
    assert own_label(ctx, 29) is None


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
        own_label(ctx, 29)
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
    monkeypatch.setattr(context_notes, "own_label", own_label)
    with pytest.raises(OperationalError) as raised:
        agent_id_note(ctx)
    assert raised.value is failure


def test_unset_machine_name_drops_only_the_machine_clause(monkeypatch: pytest.MonkeyPatch) -> None:

    from base.cluster import machine

    def _unset() -> str:
        raise machine.MachineNameMissing("unset")

    def _broken() -> str:
        raise RuntimeError("settings unreadable")

    def context(reader: Callable[[], str]) -> MeivaContext:
        return MeivaContext(
            machine=machine.MachineIdentity(
                name=reader,
                role=lambda: frozenset({"agent-runner"}),
                host=lambda: "localhost",
                description=lambda: None,
            )
        )

    assert machine_clause(context(_unset)) is None
    with pytest.raises(RuntimeError, match="settings unreadable"):
        machine_clause(context(_broken))
