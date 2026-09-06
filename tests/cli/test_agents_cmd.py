"""`ava agents` thin-client commands — each forwards to the right gateway route
and renders the response, verified without a live gateway (httpx patched).

The cmd_* functions import `httpx` and `shared.machine.gateway_api_base` inside
their bodies, so patching the module attributes here takes effect at call time.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from cli.commands import agents as _agents


class _FakeResp:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> object:
        return self._payload


def _agent_row(agent_id: int, status: str, machine: str, label: str | None) -> dict[str, object]:
    """The fields `cmd_agents_ls` reads from an /api/agents summary row."""
    return {
        "agent_id": agent_id,
        "status": status,
        "machine": machine,
        "label": label,
    }


@pytest.fixture(autouse=True)
def _gateway_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr(
        "shared.machine.gateway_auth_headers", lambda: {"Authorization": "Bearer secret"}
    )


def _patch_post(monkeypatch: pytest.MonkeyPatch, payload: object) -> dict[str, object]:
    """Patch httpx.post to record url/json and return `payload`; return the record."""
    seen: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> _FakeResp:
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        return _FakeResp(payload)

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def test_agents_ls_renders_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, object] = {}

    def fake_get(url: str, **kwargs: object) -> _FakeResp:
        seen["url"] = url
        seen["headers"] = kwargs.get("headers")
        return _FakeResp(
            [
                {
                    **_agent_row(1, "idling", "runner-a", "alpha"),
                    "workspace": "/runner/local/workspaces/agent-1",
                },
                _agent_row(22, "terminated", "runner-long", None),
            ]
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    assert _agents.cmd_agents_ls() == 0
    assert seen == {
        "url": "http://gw:8000/api/agents?fields=summary",
        "headers": {"Authorization": "Bearer secret"},
    }
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "id  status      machine      label",
        " 1  idling      runner-a     alpha",
        "22  terminated  runner-long  ",
    ]
    assert "/runner/local/workspaces" not in out


def test_agents_ls_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(httpx, "get", lambda *_a, **_k: _FakeResp([]))  # pyright: ignore[reportUnknownArgumentType]
    assert _agents.cmd_agents_ls() == 0
    assert "(no agents)" in capsys.readouterr().out


def test_agents_ls_preserves_http_error_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    def unauthorized(_url: str, **_kwargs: object) -> _FakeResp:
        return _FakeResp({"detail": "Unauthorized"}, status_code=401)

    monkeypatch.setattr(httpx, "get", unauthorized)

    with pytest.raises(httpx.HTTPStatusError):
        _agents.cmd_agents_ls()


def test_agents_cancel_posts_to_cancel_route(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # cancel uses /api/cancel with agent_id in the body, NOT /api/agents/{id}/*
    seen = _patch_post(monkeypatch, {"status": "enqueued"})
    assert _agents.cmd_agents_cancel(5) == 0
    assert seen["url"] == "http://gw:8000/api/cancel"
    assert seen["json"] == {"agent_id": 5}
    assert "cancel" in capsys.readouterr().out


def test_agents_restart_posts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch_post(monkeypatch, {"status": "enqueued"})
    assert _agents.cmd_agents_restart(9) == 0
    assert seen["url"] == "http://gw:8000/api/agents/9/restart"
    assert seen["json"] is None
    assert "enqueued" in capsys.readouterr().out


def test_agents_restart_posts_config_overlay(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch_post(monkeypatch, {"status": "enqueued"})
    assert _agents.cmd_agents_restart(9, '{"llm_model":"gpt-5.6-sol"}') == 0
    assert seen["url"] == "http://gw:8000/api/agents/9/restart"
    assert seen["json"] == {"config_overlay": {"llm_model": "gpt-5.6-sol"}}
    assert "enqueued" in capsys.readouterr().out


def test_agents_restart_rejects_non_object_config_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch_post(monkeypatch, {"status": "enqueued"})
    assert _agents.cmd_agents_restart(9, '["not", "an", "object"]') == 1
    assert "url" not in seen
    assert "config must be a JSON object" in capsys.readouterr().err


def test_agents_terminate_is_graceful(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch_post(monkeypatch, {"status": "enqueued"})
    assert _agents.cmd_agents_terminate(7) == 0
    assert seen["url"] == "http://gw:8000/api/agents/7/terminate"
    assert seen["json"] == {"force": False}
    assert "terminate" in capsys.readouterr().out


def test_agents_kill_forces(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Acceptance is asynchronous even when force interruption was requested.
    seen = _patch_post(monkeypatch, {"status": "enqueued"})
    assert _agents.cmd_agents_kill(7) == 0
    assert seen["url"] == "http://gw:8000/api/agents/7/terminate"
    assert seen["json"] == {"force": True}
    out = capsys.readouterr().out
    assert "kill" in out and "enqueued" in out
    assert "force_killed" not in out


def test_agents_send_posts_content_and_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch_post(monkeypatch, {"status": "delivered"})
    assert _agents.cmd_agents_send(5, "build done", "shell:3") == 0
    assert seen["url"] == "http://gw:8000/api/agents/5/messages"
    assert seen["json"] == {"content": "build done", "source": "shell:3"}
    assert "delivered" in capsys.readouterr().out


def test_agents_send_appends_tail_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --tail-file appends the end of the file (bounded), so the notice carries
    # the command's last output without a follow-up read.
    log = tmp_path / "out.log"
    log.write_text("early stuff\n" + "x" * 5000 + "\nFAILED: test_foo\n")
    seen = _patch_post(monkeypatch, {"status": "delivered"})
    assert _agents.cmd_agents_send(5, "exited with code 1", "shell:3", str(log)) == 0
    body = seen["json"]
    assert isinstance(body, dict)
    content = body["content"]
    assert content.startswith("exited with code 1")  # pyright: ignore[reportUnknownMemberType]
    assert "Last output" in content
    assert "FAILED: test_foo" in content
    assert "early stuff" not in content  # only the tail rides along
    assert (
        len(content) < 3000  # pyright: ignore[reportUnknownArgumentType]
    )  # bounded by the tail cap  # pyright: ignore[reportUnknownArgumentType]


def test_agents_send_missing_tail_file_still_delivers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The notice is the primary contract: an unreadable --tail-file must not
    # abort the POST — the failure rides inside the delivered message instead.
    seen = _patch_post(monkeypatch, {"status": "delivered"})
    missing = tmp_path / "gone.log"
    assert _agents.cmd_agents_send(5, "exited with code 0", "shell:3", str(missing)) == 0
    body = seen["json"]
    assert isinstance(body, dict)
    content = body["content"]
    assert content.startswith("exited with code 0")  # pyright: ignore[reportUnknownMemberType]
    assert "[tail unavailable:" in content


def test_agents_send_skips_empty_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "empty.log"
    log.write_text("   \n")
    seen = _patch_post(monkeypatch, {"status": "delivered"})
    assert _agents.cmd_agents_send(5, "msg", "shell:3", str(log)) == 0
    body = seen["json"]
    assert isinstance(body, dict)
    assert body["content"] == "msg"  # whitespace-only tail adds nothing


def test_agents_send_surfaces_error_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A server-side protocol fence must print its actionable response before
    # raising. Malformed source syntax is now rejected locally before dialing.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *_a, **_k: _FakeResp({"detail": "Unrecognized inbound source"}, status_code=422),  # pyright: ignore[reportUnknownArgumentType]
    )
    with pytest.raises(httpx.HTTPStatusError):
        _agents.cmd_agents_send(5, "msg", "external_agent:codex")
    assert "Unrecognized inbound source" in capsys.readouterr().err
