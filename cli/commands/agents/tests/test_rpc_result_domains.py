"""Actual operator commands cannot display success for unknown RPC result vocabulary."""

import httpx
import pytest

from base.host.net import http_dial
from cli.commands.agents import control


def _response(monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]) -> None:
    response = httpx.Response(200, json=payload, request=httpx.Request("POST", "http://gateway"))

    def post(*_args: object, **_kwargs: object) -> httpx.Response:
        return response

    monkeypatch.setattr(http_dial, "post", post)


@pytest.mark.parametrize("payload", [{"status": "enqueued"}, {"status": None}, {}])
def test_cancel_missing_native_acceptance_never_prints_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: dict[str, object]
) -> None:
    from cli.commands.agents.tests.test_agents_cmd import _TARGET

    target = {**_TARGET, "agent_id": 7}

    def observe(*_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=target, request=httpx.Request("GET", "http://gateway"))

    monkeypatch.setattr(http_dial, "get", observe)
    _response(monkeypatch, payload)
    with pytest.raises(ValueError):
        control.cmd_agents_cancel(7)
    assert capsys.readouterr().out == ""
