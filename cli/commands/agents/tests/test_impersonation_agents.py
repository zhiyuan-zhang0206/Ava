"""The agents-side impersonation commands: the timeline payload, the relay token channels and the relay stub write."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from cli.commands.agents import impersonation as cli
from cli.commands.agents.timeline import cmd_agents_timeline


def test_timeline_preserves_existing_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {
        "items": [{"item_id": "0.0", "kind": "system_prompt", "payload": "head"}],
        "msg_count": 100,
        "has_more": True,
    }
    seen: dict[str, Any] = {}

    def get(url: str, **kwargs: Any) -> httpx.Response:
        seen.update({"url": url, **kwargs})
        return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr("base.host.net.http_dial.get", get)
    monkeypatch.setattr("base.cluster.machine.gateway_api_base", lambda: "http://gateway")
    monkeypatch.setattr(
        "base.cluster.machine.gateway_auth_headers", lambda: {"Authorization": "Bearer cluster"}
    )
    assert cmd_agents_timeline(405) == 0
    assert seen["url"] == "http://gateway/api/agents/405/timeline"
    assert seen["params"] == {}  # no --limit: the configured gateway default applies
    assert json.loads(capsys.readouterr().out) == payload


def test_relay_token_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVA_IMPERSONATION_RELAY_TOKEN", "from-env")
    assert cli.relay_token_from_env() == "from-env"
    monkeypatch.delenv("AVA_IMPERSONATION_RELAY_TOKEN")
    with pytest.raises(ValueError, match="AVA_IMPERSONATION_RELAY_TOKEN"):
        cli.relay_token_from_env()

    def readline(*_size: object) -> str:
        return "from-stdin\n"

    monkeypatch.setattr("sys.stdin", type("Stdin", (), {"readline": readline})())
    assert cli.relay_token_from_stdin() == "from-stdin"


@pytest.mark.skipif(cli.os.name == "nt", reason="fchmod is POSIX-only")
def test_failed_relay_stub_write_removes_partial_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stub = tmp_path / ".ava-relay.env"

    def fail_mode(_fd: int, _mode: int) -> None:
        raise OSError("cannot secure stub")

    monkeypatch.setattr(cli.os, "fchmod", fail_mode)
    with pytest.raises(OSError, match="cannot secure stub"):
        cli._write_relay_stub(stub, agent_id=405, session_id=9, token="tok")  # noqa: S106
    assert not stub.exists()
