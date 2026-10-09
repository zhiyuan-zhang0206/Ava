"""Heartbeat failures stay owned until the command's existing stop/join boundary."""

from __future__ import annotations

import argparse
import asyncio
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from .. import impersonation
from .. import impersonation_relay as relay


@pytest.mark.parametrize(
    "error_type,cancelled,interrupt",
    [
        (None, False, False),
        (None, True, False),
        (None, True, True),
        (RuntimeError, False, False),
        (ValueError, False, False),
        (OSError, False, False),
        (TypeError, False, False),
    ],
)
def test_heartbeat_failure_does_not_cancel_inbox_or_change_cli_error_contract(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[Exception] | None,
    cancelled: bool,
    interrupt: bool,
) -> None:
    error = error_type("heartbeat defect") if error_type is not None else None
    events: list[str] = []
    args = argparse.Namespace(
        lease_id=str(UUID(int=1)),
        agent_id=42,
        session_id=None,
        token_stdin=False,
        provider="codex",
        thread_id="thread",
        codex_remote=None,
        debounce=0,
    )
    monkeypatch.setattr(relay, "Database", MagicMock())
    monkeypatch.setattr(relay, "EventBus", MagicMock())
    monkeypatch.setattr(impersonation, "relay_token_from_env", lambda: "test-credential")
    monkeypatch.setattr(
        "base.agents.impersonation.relay_get", MagicMock(return_value={"session_id": 0})
    )
    monkeypatch.setattr(relay, "resolve_adapter", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(relay, "_write_heartbeat", MagicMock(return_value=True))

    async def heartbeat(*_args: object) -> None:
        events.append("heartbeat_started")
        if cancelled:
            try:
                await asyncio.Event().wait()
            finally:
                events.append("heartbeat_joined")
        if error is not None:
            raise error

    async def inbox(*_args: object, **_kwargs: object) -> None:
        for _ in range(4):
            await asyncio.sleep(0)
        assert events == ["heartbeat_started"]
        events.append("inbox_finished")
        if interrupt:
            raise KeyboardInterrupt

    monkeypatch.setattr(relay, "_heartbeat_loop", heartbeat)
    monkeypatch.setattr(relay, "relay_inbox", inbox)
    monkeypatch.setattr(relay.base.events.live.redis_listener, "RedisInboundListener", MagicMock())

    if interrupt:
        assert relay.cmd_relay(args) == 130
        assert capsys.readouterr().err == (
            "Relay stopped; pending messages are unchanged and the lease is not renewed.\n"
        )
    elif error_type is TypeError:
        with pytest.raises(TypeError) as caught:
            relay.cmd_relay(args)
        assert caught.value is error
    else:
        assert relay.cmd_relay(args) == (0 if error is None else 1)
        if error is not None:
            assert capsys.readouterr().err == "Impersonation relay stopped: heartbeat defect\n"
    assert events == ["heartbeat_started", "inbox_finished"] + (
        ["heartbeat_joined"] if cancelled else []
    )
