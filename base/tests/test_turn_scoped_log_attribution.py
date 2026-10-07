"""Turn-scoped log attribution — which agent a log record belongs to.

Prerequisite 3 of `future/infra/lifecycle/agent-runner-as-server.md` Phase 1. The hosted
runner writes log records for every local agent from one process, so a fixed
`agent_id` frozen into loguru's `extra` would stamp every record with the same
agent. The default binding is a
`base.native_process.turn_identity.TurnScopedAgentId` resolved per record from
the turn contextvar.

Locked here: turn scoping (a bound turn wins), explicit `agent_id=` still
winning, the `-` no-agent sentinel outside a turn, the human/JSONL renderings
resolving rather than printing an object repr, and the same turn-first order in
`base.telemetry.emit`'s ambient fallback.
"""

from __future__ import annotations

from unittest import mock

import pytest

from base import telemetry
from base.log import _message_to_params
from base.native_process.turn_identity import (
    TURN_SCOPED_AGENT_ID,
    TurnScopedAgentId,
    bind_turn_identity,
)


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
    def test_turn_binding_wins(self) -> None:
        with bind_turn_identity(42):
            assert TurnScopedAgentId().resolve() == "42"
            assert _agent_id_of({"agent_id": TURN_SCOPED_AGENT_ID, "event": "log"}) == 42
        assert _agent_id_of({"agent_id": TURN_SCOPED_AGENT_ID, "event": "log"}) is None

    def test_no_turn_is_the_dash_sentinel(self) -> None:
        """A host process binds no agent of its own; a record written outside
        any turn (the host's own bookkeeping) stays unattributed."""
        assert TurnScopedAgentId().resolve() == "-"
        assert _agent_id_of({"agent_id": TURN_SCOPED_AGENT_ID, "event": "log"}) is None

    def test_explicit_agent_id_still_wins(self) -> None:
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
        the deferred object costs an ordinary daemon nothing — with no turn it
        still resolves to `"-"`.

        Asserted through the init function rather than by reading the live
        logger's `extra`: `logger.configure` REPLACES the whole dict and several
        inits call it, so a global read is order-dependent (issue #147's bug
        class) and would pass or fail on which sibling ran first.
        """
        import base.log as slog

        monkeypatch.setattr(slog, "_init_done", False)
        with (
            mock.patch.object(slog.logger, "add"),
            mock.patch.object(slog, "_add_file_sink"),
            mock.patch.object(slog, "add_postgres_sink"),
            mock.patch.object(slog, "_install_stdlib_intercept"),
            mock.patch.object(slog.logger, "info"),
            mock.patch.object(slog.logger, "configure") as configure,
        ):
            slog.init_gateway_process(name="agent_host")

        configure.assert_called_once()
        bound = configure.call_args.kwargs["extra"]["agent_id"]
        assert isinstance(bound, TurnScopedAgentId)


class TestRendering:
    def test_format_spec_applies_to_the_resolved_value(self) -> None:
        # The human stderr format is `a={extra[agent_id]:>3}`.
        assert f"{TURN_SCOPED_AGENT_ID:>3}" == "  -"
        with bind_turn_identity(7):
            assert f"{TURN_SCOPED_AGENT_ID:>3}" == "  7"
        with bind_turn_identity(1234):
            assert f"{TURN_SCOPED_AGENT_ID:>3}" == "1234"

    def test_str_resolves_for_the_jsonl_sink(self) -> None:
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
