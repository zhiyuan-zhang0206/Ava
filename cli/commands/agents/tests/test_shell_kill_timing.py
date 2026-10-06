"""The operator must not display an unknown cleanup timing as completed."""

import httpx
import pytest

from base.host.net import http_dial
from cli.commands.agents import control


@pytest.mark.parametrize(
    ("shell", "message"),
    [
        ({"when": "at_exit", "killed": []}, "shell sessions are killed when it exits"),
        ({"when": "now", "killed": [2, 5]}, "killed 2 shell session(s): 2, 5"),
        ({"when": "now", "killed": []}, "no shell sessions to kill"),
        (None, "shell sessions NOT killed (its runner predates the option)"),
    ],
)
def test_terminate_renders_supported_wire_timing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    shell: dict[str, object] | None,
    message: str,
) -> None:
    response = httpx.Response(
        200,
        json={"status": "enqueued", "shell_sessions": shell},
        request=httpx.Request("POST", "http://gateway/api/agents/7/terminate"),
    )

    def post(*_args: object, **_kwargs: object) -> httpx.Response:
        return response

    monkeypatch.setattr(http_dial, "post", post)
    assert control.cmd_agents_terminate(7, kill_all_shell_sessions=True) == 0
    assert message in capsys.readouterr().out


@pytest.mark.parametrize("when", ["later", "", None])
def test_terminate_does_not_claim_unknown_cleanup_completed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    when: object,
) -> None:
    response = httpx.Response(
        200,
        json={"status": "enqueued", "shell_sessions": {"when": when, "killed": [2]}},
        request=httpx.Request("POST", "http://gateway/api/agents/7/terminate"),
    )

    def post(*_args: object, **_kwargs: object) -> httpx.Response:
        return response

    monkeypatch.setattr(http_dial, "post", post)
    with pytest.raises(ValueError, match="ShellSessionKillTiming"):
        control.cmd_agents_terminate(7, kill_all_shell_sessions=True)
    assert capsys.readouterr().out == ""
