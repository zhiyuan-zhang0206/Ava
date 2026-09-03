"""Turn-scoped log attribution — which agent a log record belongs to.

Prerequisite 3 of `future/infra/agent-runner-as-server.md` Phase 1.
`init_agent_process` used to freeze the agent id into loguru's `extra` at
process boot, which is exact for one agent per process and wrong for the hosted
runner: every record the process writes would carry whichever agent booted it.
The binding is now a `shared.turn_identity.TurnScopedAgentId` resolved per
record — turn contextvar first, this process's agent second.

Locked here: process-mode equivalence (the resolved value is the boot agent),
turn scoping (a bound turn wins), explicit `agent_id=` still winning over both,
the `-` no-agent sentinel surviving, the human/JSONL renderings resolving rather
than printing an object repr, the attribution slot staying independent of
`effective_agent_id`'s env channel, and the same three-layer order in
`shared.telemetry.emit`'s ambient fallback.
"""

from __future__ import annotations

from unittest import mock

import pytest

from shared import telemetry, turn_identity
from shared.log import _message_to_params
from shared.turn_identity import (
    TURN_SCOPED_AGENT_ID,
    TurnScopedAgentId,
    bind_turn_identity,
    set_process_agent_id,
)


@pytest.fixture
def process_agent(monkeypatch: pytest.MonkeyPatch):
    """Pretend this process booted as agent 7 (what init_agent_process does)."""
    monkeypatch.setattr(turn_identity, "_process_agent_id", 7)


class _FakeMessage:
    """The shape `_message_to_params` reads off a loguru message."""

    def __init__(self, extra: dict[str, object]) -> None:
        from datetime import UTC, datetime

        self.record = {
            "time": datetime.now(UTC),
            "extra": extra,
            "message": "hello",
            "exception": None,
            "level": type("L", (), {"name": "INFO"})(),
        }


def _agent_id_of(extra: dict[str, object]) -> int | None:
    _ts, agent_id, _level, _event, _payload, _source = _message_to_params(_FakeMessage(extra))  # pyright: ignore[reportArgumentType]
    return agent_id


class TestResolution:
    def test_unbound_resolves_to_the_process_agent(self, process_agent) -> None:
        assert TurnScopedAgentId().resolve() == "7"
        assert _agent_id_of({"agent_id": TURN_SCOPED_AGENT_ID, "event": "log"}) == 7

    def test_turn_binding_wins(self, process_agent) -> None:
        with bind_turn_identity(42):
            assert _agent_id_of({"agent_id": TURN_SCOPED_AGENT_ID, "event": "log"}) == 42
        assert _agent_id_of({"agent_id": TURN_SCOPED_AGENT_ID, "event": "log"}) == 7

    def test_no_process_agent_and_no_turn_is_the_dash_sentinel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host process binds no agent of its own; a record written outside
        any turn (the host's own bookkeeping) stays unattributed."""
        monkeypatch.setattr(turn_identity, "_process_agent_id", None)
        assert TurnScopedAgentId().resolve() == "-"
        assert _agent_id_of({"agent_id": TURN_SCOPED_AGENT_ID, "event": "log"}) is None

    def test_explicit_agent_id_still_wins(self, process_agent) -> None:
        """`logger.bind(agent_id=N)` replaces the extra value outright, so it
        never reaches the deferred binding — attribution stays explicit."""
        with bind_turn_identity(42):
            assert _agent_id_of({"agent_id": "99", "event": "log"}) == 99

    def test_transport_source_preserves_a_usage_payload_source(self) -> None:
        """`llm_usage.source` is an accounting dimension, not event provenance."""
        _ts, _agent_id, _level, _event, payload, source = _message_to_params(
            _FakeMessage(  # pyright: ignore[reportArgumentType]
                {
                    "agent_id": "-",
                    "event": "llm_usage",
                    "source": "web.fetch",
                    "transport_source": "system",
                }
            )
        )

        assert source == "system"
        assert payload["source"] == "web.fetch"


class TestDefaultBinding:
    def test_init_gateway_process_binds_the_deferred_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A daemon-style init must bind the deferred object, not a bare `"-"`.

        The hosted agent-runner inits through `init_gateway_process`. A static
        sentinel there stamps EVERY hosted agent's record with `-`, discarding
        attribution the turn contextvar is holding at that very moment. Binding
        the deferred object costs an ordinary daemon nothing — with no turn and
        no process agent it still resolves to `"-"`.

        Asserted through the init function rather than by reading the live
        logger's `extra`: `logger.configure` REPLACES the whole dict and several
        inits call it, so a global read is order-dependent (issue #147's bug
        class) and would pass or fail on which sibling ran first.
        """
        import shared.log as slog

        monkeypatch.setattr(slog, "_init_done", False)
        with (
            mock.patch.object(slog.logger, "add"),
            mock.patch.object(slog, "_add_file_sink"),
            mock.patch.object(slog, "_add_postgres_sink"),
            mock.patch.object(slog, "_install_stdlib_intercept"),
            mock.patch.object(slog.logger, "info"),
            mock.patch.object(slog.logger, "configure") as configure,
        ):
            slog.init_gateway_process(name="agent_host")

        configure.assert_called_once()
        bound = configure.call_args.kwargs["extra"]["agent_id"]
        assert isinstance(bound, TurnScopedAgentId)

    def test_daemon_style_process_attributes_a_bound_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hosted case end to end at the record level: no process agent (a
        daemon init), a turn bound, so the record belongs to that turn's agent —
        and outside the turn the same process is back to unattributed."""
        monkeypatch.setattr(turn_identity, "_process_agent_id", None)

        with bind_turn_identity(314):
            assert _agent_id_of({"agent_id": TURN_SCOPED_AGENT_ID, "event": "log"}) == 314
        assert _agent_id_of({"agent_id": TURN_SCOPED_AGENT_ID, "event": "log"}) is None


class TestProcessSlot:
    def test_setter_is_what_the_resolution_reads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`init_agent_process` declares the process's agent through the setter;
        restore it afterwards so a real init in this process is not disturbed."""
        monkeypatch.setattr(turn_identity, "_process_agent_id", None)
        set_process_agent_id(11)
        assert TurnScopedAgentId().resolve() == "11"
        set_process_agent_id(None)
        assert TurnScopedAgentId().resolve() == "-"

    def test_slot_does_not_feed_effective_agent_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The two reads answer different questions — "whose log file is this"
        vs "who is executing" — so the attribution slot must not leak into the
        execution read, whose second layer is the AVA_AGENT_ID env channel."""
        monkeypatch.delenv("AVA_AGENT_ID", raising=False)
        monkeypatch.setattr(turn_identity, "_process_agent_id", 7)
        assert turn_identity.effective_agent_id() is None
        with bind_turn_identity(42):
            assert turn_identity.effective_agent_id() == 42


class TestRendering:
    def test_format_spec_applies_to_the_resolved_value(self, process_agent) -> None:
        # The human stderr format is `a={extra[agent_id]:>3}`.
        assert f"{TURN_SCOPED_AGENT_ID:>3}" == "  7"
        with bind_turn_identity(1234):
            assert f"{TURN_SCOPED_AGENT_ID:>3}" == "1234"

    def test_str_resolves_for_the_jsonl_sink(self, process_agent) -> None:
        # loguru's serialize=True dumps extra with `default=str`.
        import json

        with bind_turn_identity(42):
            dumped = json.dumps({"agent_id": TURN_SCOPED_AGENT_ID}, default=str)
        assert json.loads(dumped) == {"agent_id": "42"}


class TestTelemetryAmbient:
    def test_turn_wins_over_the_process_binding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(telemetry._state, "agent_id", 7)
        assert telemetry._ambient_agent_id() == 7
        with bind_turn_identity(42):
            assert telemetry._ambient_agent_id() == 42

    def test_unbound_process_falls_through_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(telemetry._state, "agent_id", None)
        assert telemetry._ambient_agent_id() is None
