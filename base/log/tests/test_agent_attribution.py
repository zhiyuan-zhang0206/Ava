"""Ordinary logs use explicit caller or process attribution, never a turn fallback."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest

import ava
from base.agents.context.identity import AgentIdentity
from base.log import _message_to_params


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


def test_unannotated_log_has_no_agent() -> None:
    ava.context = replace(ava.context, identity=AgentIdentity(42, True))
    assert _agent_id_of({"agent_id": "-", "event": "log"}) is None


def test_explicit_agent_id_is_preserved() -> None:
    ava.context = replace(ava.context, identity=AgentIdentity(42, True))
    assert _agent_id_of({"agent_id": "99", "event": "log"}) == 99


def test_missing_required_log_field_fails() -> None:
    with pytest.raises(KeyError, match="agent_id"):
        _agent_id_of({"event": "log"})


def test_transport_source_preserves_usage_payload_source() -> None:
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


def test_gateway_boot_retains_process_generation_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    import base.log as slog

    monkeypatch.setattr(slog, "_init_done", False)
    ava.context = replace(ava.context, identity=AgentIdentity(42, True))
    with (
        mock.patch.object(slog.logger, "add"),
        mock.patch.object(slog, "_add_file_sink"),
        mock.patch.object(slog, "add_postgres_sink") as init_pipeline,
        mock.patch.object(slog, "_install_stdlib_intercept"),
        mock.patch.object(slog.loaded_commit, "freeze") as freeze,
        mock.patch.object(slog.loaded_commit, "get", return_value="loaded-generation"),
        mock.patch.object(slog, "_machine_name_lazy", return_value="machine-a"),
        mock.patch.object(slog.logger, "info") as boot_log,
        mock.patch.object(slog.logger, "configure") as configure,
    ):
        slog.init_gateway_process(
            name="agent_host",
        )

    configure.assert_called_once_with(extra={"agent_id": "-"})
    init_pipeline.assert_called_once_with(process="agent_host")
    freeze.assert_called_once_with()
    fields = boot_log.call_args.kwargs
    assert fields["event"] == "service_started"
    assert fields["name"] == "agent_host"
    assert fields["sha"] == "loaded-generation"
    assert fields["host"] == "machine-a"
    assert fields["pid"] > 0


def test_owned_gateway_boot_uses_loaded_image_and_original_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base.log as slog

    monkeypatch.setattr(slog, "_init_done", False)
    producer = mock.Mock(side_effect=AssertionError("the logger must pass its root's factory"))
    machine = mock.Mock(return_value="owned-machine")
    image = cast(Any, SimpleNamespace(sha="already-loaded"))
    with (
        mock.patch.object(slog.logger, "add"),
        mock.patch.object(slog, "_add_file_sink"),
        mock.patch.object(slog, "add_postgres_sink") as init_pipeline,
        mock.patch.object(slog, "_install_stdlib_intercept"),
        mock.patch.object(slog.loaded_commit, "freeze") as freeze,
        mock.patch.object(slog.loaded_commit, "get") as read_late_version,
        mock.patch.object(slog.logger, "info") as boot_log,
    ):
        slog.init_gateway_process(
            name="agent_host", producer=producer, machine_reader=machine, image=image
        )
    init_pipeline.assert_called_once_with(
        process="agent_host", producer=producer, machine_reader=machine
    )
    producer.assert_not_called()
    freeze.assert_not_called()
    read_late_version.assert_not_called()
    assert boot_log.call_args.kwargs["sha"] == "already-loaded"
    assert boot_log.call_args.kwargs["host"] == "owned-machine"
