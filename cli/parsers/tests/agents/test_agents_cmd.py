"""`ava agents` thin-client commands — each forwards to the right gateway route
and renders the response, verified without a live gateway (httpx patched).

The cmd_* functions import `httpx` and `base.cluster.machine.gateway_api_base` inside
their bodies, so patching the module attributes here takes effect at call time.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import base.cluster.machine as cluster_machine
from base.cluster.machine.tests.gateway_stubs import (
    gateway_api_base_stub,
    gateway_auth_headers_stub,
)
from cli.commands.agents import control as _agents

TARGET = {
    "agent_id": 5,
    "work_id": "00000000-0000-4000-8000-000000000001",
    "machine": "local",
    "generation": "00000000-0000-4000-8000-000000000002",
    "owner": "00000000-0000-4000-8000-000000000003",
    "protocol": 1,
}
_COMPACT_TARGET = {
    "protocol": 1,
    "observation_id": "00000000-0000-4000-8000-000000000004",
    "source": {**TARGET, "agent_id": 7},
    "checkpoint_id": "source",
    "checkpoint_ns": "",
    "messages_version": "1",
    "compact_channel_version": None,
    "segment_version": 0,
    "model": "gpt-5.6-sol",
}


class _FakeResp:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> object:
        return self._payload


def _agent_row(agent_id: int, status: str, machine: str, label: str | None) -> dict[str, object]:
    """The fields `cmd_agents_ls` reads from an agent directory card."""
    return {
        "agent_id": agent_id,
        "status": status,
        "machine": machine,
        "label": label,
    }


@pytest.fixture(autouse=True)
def _gateway_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cluster_machine, "gateway_api_base", gateway_api_base_stub("http://gw:8000")
    )
    monkeypatch.setattr(
        cluster_machine,
        "gateway_auth_headers",
        gateway_auth_headers_stub({"Authorization": "Bearer secret"}),
    )


def _patch_post(monkeypatch: pytest.MonkeyPatch, payload: object) -> dict[str, object]:
    """Patch httpx.post to record url/json and return `payload`; return the record."""
    seen: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> _FakeResp:
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        seen["headers"] = kwargs.get("headers")
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
        seen["params"] = kwargs.get("params")
        return _FakeResp(
            {
                "agents": [
                    {
                        **_agent_row(1, "idling", "runner-a", "alpha"),
                        "workspace": "/runner/local/workspaces/agent-1",
                    },
                    _agent_row(22, "terminated", "runner-long", None),
                ],
                "next_cursor": None,
            }
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    assert _agents.cmd_agents_ls() == 0
    assert seen == {
        "url": "http://gw:8000/api/agents",
        "params": {"scope": "live", "query": "", "limit": 100},
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
    def fake_get(*_args: object, **_kwargs: object) -> _FakeResp:
        return _FakeResp({"agents": [], "next_cursor": None})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert _agents.cmd_agents_ls() == 0
    assert "(no agents)" in capsys.readouterr().out


def test_agents_ls_passes_page_arguments_and_prints_cursor(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, object]] = []

    def fake_get(_url: str, **kwargs: object) -> _FakeResp:
        calls.append(kwargs)
        return _FakeResp(
            {
                "agents": [_agent_row(42, "terminated", "mini", "research")],
                "next_cursor": 42,
            }
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    assert _agents.cmd_agents_ls(scope="terminated", query="research", before_id=99, limit=1) == 0
    assert len(calls) == 1
    assert calls[0]["params"] == {
        "scope": "terminated",
        "query": "research",
        "before_id": 99,
        "limit": 1,
    }
    assert "repeat with --before-id 42" in capsys.readouterr().out


def test_agents_ls_parser_passes_page_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from cli.commands.agents.parsers import add_agents_parser

    parser = argparse.ArgumentParser()
    add_agents_parser(parser.add_subparsers())
    args = parser.parse_args(
        [
            "agents",
            "ls",
            "--scope",
            "terminated",
            "--query",
            "research",
            "--before-id",
            "42",
            "--limit",
            "5",
        ]
    )
    seen: dict[str, object] = {}

    def fake_command(**kwargs: object) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(_agents, "cmd_agents_ls", fake_command)
    assert args.func(args) == 0
    assert seen == {"scope": "terminated", "query": "research", "before_id": 42, "limit": 5}


def test_agents_ls_preserves_http_error_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    def unauthorized(_url: str, **_kwargs: object) -> _FakeResp:
        return _FakeResp({"detail": "Unauthorized"}, status_code=401)

    monkeypatch.setattr(httpx, "get", unauthorized)

    with pytest.raises(httpx.HTTPStatusError):
        _agents.cmd_agents_ls()


def test_agents_cancel_posts_to_cancel_route(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def observe(*_args: object, **_kwargs: object) -> _FakeResp:
        return _FakeResp(TARGET)

    monkeypatch.setattr(httpx, "get", observe)
    seen = _patch_post(monkeypatch, {"target": TARGET, "command_id": TARGET["work_id"]})
    assert _agents.cmd_agents_cancel(5) == 0
    assert seen["url"] == "http://gw:8000/api/keyed/v1/agents/5/cancel-work"
    assert isinstance(seen["headers"], dict)
    assert seen["headers"]["Idempotency-Key"]
    assert seen["headers"]["Idempotency-Scope"] == "principal-v1"
    assert seen["json"] == TARGET
    assert "cancel accepted" in capsys.readouterr().out


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


def test_agents_terminate_kill_all_shell_sessions_sends_the_option(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch_post(
        monkeypatch,
        {"status": "enqueued", "shell_sessions": {"when": "at_exit", "killed": []}},
    )
    assert _agents.cmd_agents_terminate(7, kill_all_shell_sessions=True) == 0
    assert seen["url"] == "http://gw:8000/api/agents/7/terminate"
    assert seen["json"] == {"force": False, "kill_all_shell_sessions": True}
    assert "shell sessions are killed when it exits" in capsys.readouterr().out


def test_agents_kill_kill_all_shell_sessions_sends_the_option(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch_post(
        monkeypatch,
        {"status": "enqueued", "shell_sessions": {"when": "now", "killed": [0, 3]}},
    )
    assert _agents.cmd_agents_kill(7, kill_all_shell_sessions=True) == 0
    assert seen["url"] == "http://gw:8000/api/agents/7/terminate"
    assert seen["json"] == {"force": True, "kill_all_shell_sessions": True}
    assert "killed 2 shell session(s): 0, 3" in capsys.readouterr().out


def test_agents_parser_wires_kill_all_shell_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli.parsers import build_parser

    seen: dict[str, object] = {}

    def _record(agent_id: int, *, source: str | None, kill_all_shell_sessions: bool) -> int:
        seen[str(agent_id)] = kill_all_shell_sessions
        return 0

    monkeypatch.setattr(_agents, "cmd_agents_terminate", _record)
    monkeypatch.setattr(_agents, "cmd_agents_kill", _record)
    for argv in (
        ["agents", "terminate", "7", "--kill-all-shell-sessions"],
        ["agents", "kill", "8", "--kill-all-shell-sessions"],
        ["agents", "terminate", "9"],
    ):
        args = build_parser().parse_args(argv)
        assert args.func(args) == 0
    assert seen == {"7": True, "8": True, "9": False}


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
    log.write_text("early stuff\nmiddle\n" + "x" * 5000 + "\nline a\nline b\nFAILED: test_foo\n")
    seen = _patch_post(monkeypatch, {"status": "delivered"})
    assert _agents.cmd_agents_send(5, "finished", "shell:3", str(log)) == 0
    body = seen["json"]
    assert isinstance(body, dict)
    content = body["content"]
    assert content.startswith("finished")  # pyright: ignore[reportUnknownMemberType]
    assert "Last output" in content
    assert "FAILED: test_foo" in content
    assert "early stuff" not in content  # only the last 3 lines ride along
    assert "line a" in content and "x" * 100 not in content
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
    assert _agents.cmd_agents_send(5, "finished", "shell:3", str(missing)) == 0
    body = seen["json"]
    assert isinstance(body, dict)
    content = body["content"]
    assert content.startswith("finished")  # pyright: ignore[reportUnknownMemberType]
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


# ── deferred-delivery outbox coverage on the send path (task #3769) ──────────


def test_agents_terminate_renders_the_shell_session_kill(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The kill report is rendered; a requested kill with no report (a runner
    that predates the option) is called out; no request stays silent."""
    _patch_post(
        monkeypatch,
        {"status": "already_terminated", "shell_sessions": {"when": "now", "killed": []}},
    )
    assert _agents.cmd_agents_terminate(7, kill_all_shell_sessions=True) == 0
    assert "no shell sessions to kill" in capsys.readouterr().out

    _patch_post(monkeypatch, {"status": "enqueued"})
    assert _agents.cmd_agents_terminate(7, kill_all_shell_sessions=True) == 0
    assert "shell sessions NOT killed" in capsys.readouterr().out

    _patch_post(monkeypatch, {"status": "enqueued", "shell_sessions": None})
    assert _agents.cmd_agents_terminate(7) == 0
    assert "shell" not in capsys.readouterr().out


def test_agents_compact_posts_to_the_compact_route(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def observe(*_args: object, **_kwargs: object) -> _FakeResp:
        return _FakeResp(_COMPACT_TARGET)

    monkeypatch.setattr(httpx, "get", observe)
    seen = _patch_post(monkeypatch, {"target": _COMPACT_TARGET, "command_id": TARGET["work_id"]})
    assert _agents.cmd_agents_compact(7) == 0
    assert seen["url"] == "http://gw:8000/api/keyed/v1/agents/7/compact-history"
    assert isinstance(seen["headers"], dict)
    assert seen["headers"]["Idempotency-Key"]
    assert seen["headers"]["Idempotency-Scope"] == "principal-v1"
    assert seen["json"] == _COMPACT_TARGET
    assert "compact accepted" in capsys.readouterr().out


def test_agents_compact_parser_dispatches() -> None:
    import argparse

    from cli.commands.agents.parsers import add_agents_parser

    parser = argparse.ArgumentParser()
    add_agents_parser(parser.add_subparsers())
    args = parser.parse_args(["agents", "compact", "7"])
    assert args.agent_id == 7
    assert args.func.__name__ == "_h_agents_compact"
