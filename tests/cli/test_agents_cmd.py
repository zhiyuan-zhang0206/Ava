"""`ava agents` thin-client commands — each forwards to the right gateway route
and renders the response, verified without a live gateway (httpx patched).

The cmd_* functions import `httpx` and `shared.machine.gateway_api_base` inside
their bodies, so patching the module attributes here takes effect at call time.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from cli.commands import agents as _agents


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
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr(
        "shared.machine.gateway_auth_headers", lambda: {"Authorization": "Bearer secret"}
    )


@pytest.fixture(autouse=True)
def _outbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Send tests exercise the real outbox glue against a throwaway $AVA_HOME —
    never the operator's live journal — with the knobs stubbed so no config or
    dotenv read is involved."""
    from shared import delivery_outbox
    from shared.config import settings

    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path))
    monkeypatch.setattr(
        delivery_outbox,
        "limits",
        lambda: delivery_outbox.DeliveryOutboxLimits(
            enabled=True,
            retry_backoff_steps=(30.0, 60.0, 300.0, 900.0),
            budget_seconds=43200.0,
            dedup_window_seconds=900.0,
            flush_interval_seconds=30.0,
            max_entries=128,
        ),
    )
    delivery_outbox._reset_caches_for_tests()
    yield
    delivery_outbox._reset_caches_for_tests()


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

    from cli.parsers.agents import _add_agents_parser

    parser = argparse.ArgumentParser()
    _add_agents_parser(parser.add_subparsers())
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


# ── deferred-delivery outbox coverage on the send path (task #3769) ──────────


def test_agents_send_transport_failure_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A refused connection records the notice under the shared key (with the
    Idempotency-Key header on the attempt) and still raises — the CLI's exit
    semantics are untouched."""
    recorded: list[dict[str, object]] = []
    seen: dict[str, object] = {}

    def fake_logical_key(**_kw: object) -> str:
        return "key-cli-1"

    def fake_record(**kw: object) -> None:
        recorded.append(kw)

    def fail_post(url: str, **kwargs: object) -> object:
        seen.update(kwargs)
        raise httpx.ConnectError("gateway down")

    monkeypatch.setattr("shared.delivery_outbox.logical_key", fake_logical_key)
    monkeypatch.setattr("shared.delivery_outbox.record_failed_send", fake_record)
    monkeypatch.setattr(httpx, "post", fail_post)
    with pytest.raises(httpx.ConnectError):
        _agents.cmd_agents_send(5, "notice", "shell:3")
    assert recorded == [
        {
            "agent_id": 5,
            "source": "shell:3",
            "content": "notice",
            "client_message_id": "key-cli-1",
        }
    ]
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["Idempotency-Key"] == "key-cli-1"


def test_agents_send_transient_http_is_recorded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """429/5xx is the replayable failure class: it is recorded, and the
    response body is still surfaced before the raise."""
    recorded: list[dict[str, object]] = []

    def fake_logical_key(**_kw: object) -> str:
        return "key-cli-1"

    def fake_record(**kw: object) -> None:
        recorded.append(kw)

    def fake_post(*_a: object, **_k: object) -> _FakeResp:
        return _FakeResp({"detail": "backend down"}, status_code=503)

    monkeypatch.setattr("shared.delivery_outbox.logical_key", fake_logical_key)
    monkeypatch.setattr("shared.delivery_outbox.record_failed_send", fake_record)
    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(httpx.HTTPStatusError):
        _agents.cmd_agents_send(5, "notice", "shell:3")
    assert [kw["client_message_id"] for kw in recorded] == ["key-cli-1"]
    assert "backend down" in capsys.readouterr().err


def test_agents_send_success_retires_the_pending_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delivered notice retires any pending record for the same logical
    message, and carries the shared key as its Idempotency-Key."""
    retired: list[dict[str, object]] = []

    def fake_logical_key(**_kw: object) -> str:
        return "key-cli-1"

    def fake_retire(**kw: object) -> None:
        retired.append(kw)

    monkeypatch.setattr("shared.delivery_outbox.logical_key", fake_logical_key)
    monkeypatch.setattr("shared.delivery_outbox.note_send_succeeded", fake_retire)
    seen = _patch_post(monkeypatch, {"status": "delivered"})
    assert _agents.cmd_agents_send(5, "build done", "shell:3") == 0
    assert retired == [
        {"agent_id": 5, "source": "shell:3", "content": "build done", "key": "key-cli-1"}
    ]
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["Idempotency-Key"] == "key-cli-1"


def test_agents_send_client_error_records_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 4xx is application semantics — replay cannot change it, so nothing is
    recorded and nothing is retired."""
    calls: list[dict[str, object]] = []

    def fake_logical_key(**_kw: object) -> str:
        return "key-cli-1"

    def fake_call(**kw: object) -> None:
        calls.append(kw)

    def fake_post(*_a: object, **_k: object) -> _FakeResp:
        return _FakeResp({"detail": "Unrecognized inbound source"}, status_code=422)

    monkeypatch.setattr("shared.delivery_outbox.logical_key", fake_logical_key)
    monkeypatch.setattr("shared.delivery_outbox.record_failed_send", fake_call)
    monkeypatch.setattr("shared.delivery_outbox.note_send_succeeded", fake_call)
    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(httpx.HTTPStatusError):
        _agents.cmd_agents_send(5, "msg", "external_agent:codex")
    assert calls == []


def test_agents_send_degrades_to_unkeyed_when_outbox_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unusable outbox must not change the send's outcome: the notice goes
    out without a key and the degradation is named on stderr."""

    def boom(**_kw: object) -> str:
        raise RuntimeError("outbox broken")

    monkeypatch.setattr("shared.delivery_outbox.logical_key", boom)
    seen = _patch_post(monkeypatch, {"status": "delivered"})
    assert _agents.cmd_agents_send(5, "build done", "shell:3") == 0
    assert "unkeyed" in capsys.readouterr().err
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert "Idempotency-Key" not in headers
