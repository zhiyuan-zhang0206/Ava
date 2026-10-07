"""Provider wire formats, bound targets, failures and terminal-notice ownership."""

from __future__ import annotations

import subprocess
from uuid import UUID

import pytest

from cli.commands.agents import impersonation_adapters as adapters

THREAD_ID = UUID("b9d32d0d-bd27-40fc-83e8-692769b21523")


def test_codex_requires_a_control_endpoint_without_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base.agents.impersonation import host_transport as codex_app_server

    monkeypatch.setattr(codex_app_server, "default_control_endpoint", lambda: None)
    with pytest.raises(RuntimeError, match=r"Steer.*--codex-remote"):
        adapters.resolve_adapter("codex", str(THREAD_ID))


@pytest.mark.parametrize("failure", ["ActiveTurnNotSteerable", "TimeoutError", "unknown thread"])
def test_codex_refusal_never_falls_back_to_pending(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from unittest.mock import Mock

    queued = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr("base.host.proc.run_bounded", queued)

    def refuse(_thread_id: str, _message: str, *, endpoint: str) -> str:
        return failure

    monkeypatch.setattr(adapters, "live_submit", refuse)
    adapter = adapters.resolve_adapter(
        "codex", str(THREAD_ID), codex_remote="unix:///tmp/ava-codex.sock"
    )
    with pytest.raises(RuntimeError, match="Steer delivery failed"):
        adapter.send("push")
    queued.assert_not_called()


@pytest.mark.parametrize("explicit", [True, False])
def test_codex_emitter_delivers_literal_input_to_the_owning_server(
    monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    from base.agents.impersonation import host_transport as codex_app_server

    attempts: list[tuple[str, str, str]] = []
    endpoint = "unix:///tmp/ava-codex.sock"
    message = "Ava push with literal $(no-shell) and `no-shell`"

    def delivered_live(thread_id: str, text: str, *, endpoint: str) -> None:
        attempts.append((thread_id, text, endpoint))

    monkeypatch.setattr(codex_app_server, "default_control_endpoint", lambda: endpoint)
    monkeypatch.setattr(adapters, "live_submit", delivered_live)
    adapters.resolve_adapter(
        "codex", str(THREAD_ID), codex_remote=endpoint if explicit else None
    ).send(message)
    assert attempts == [(str(THREAD_ID), message, endpoint)]


@pytest.mark.parametrize("provider", ["claude", "dsh"])
def test_controller_adapter_rejects_codex_remote(provider: str) -> None:
    with pytest.raises(ValueError, match="--codex-remote"):
        adapters.resolve_adapter(provider, None, codex_remote="unix:///tmp/codex.sock")


@pytest.mark.parametrize(
    "provider,thread_id", [("codex", None), ("claude", "thread"), ("dsh", "thread"), ("?", None)]
)
def test_host_target_must_be_explicit(provider: str, thread_id: str | None) -> None:
    with pytest.raises(ValueError):
        adapters.resolve_adapter(provider, thread_id)


@pytest.mark.parametrize("provider", ["claude", "dsh"])
def test_controller_adapter_keeps_multiline_unicode_envelopes(
    provider: str, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    message = 'Ava message agent=42\n[id=7] kind=chat\nquoted "text" and café'
    adapter = adapters.resolve_adapter(provider, None)
    assert adapter.notify_terminal
    adapter.send(message)
    output = capsys.readouterr().out
    if provider == "dsh":
        assert len(output.splitlines()) == 1
        assert json.loads(output) == message
    else:
        assert output == message + "\n"


def test_codex_terminal_notice_has_one_native_owner() -> None:
    adapter = adapters.resolve_adapter(
        "codex", str(THREAD_ID), codex_remote="unix:///tmp/ava-codex.sock"
    )
    assert not adapter.notify_terminal
