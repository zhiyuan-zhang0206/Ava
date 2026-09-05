"""Thin CLI aliases and explicit controller operations."""

# ruff: noqa: S105 — fixture-only credential

from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from cli.commands import impersonation as cli
from cli.commands.agent_timeline import cmd_agents_timeline
from cli.parsers import build_parser
from shared import impersonation as control


def _args(*args: str) -> Namespace:
    return build_parser().parse_args(["impersonate", *args])


def test_timeline_and_context_are_one_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int, str | None]] = []

    def timeline(agent_id: int, limit: int, before: str | None) -> int:
        calls.append((agent_id, limit, before))
        return 0

    monkeypatch.setattr("cli.commands.agent_timeline.cmd_agents_timeline", timeline)
    for name in ("timeline", "context"):
        args = build_parser().parse_args(
            ["agents", name, "405", "--limit", "100", "--before", "12.0"]
        )
        assert args.func(args) == 0
    assert calls == [(405, 100, "12.0"), (405, 100, "12.0")]


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

    monkeypatch.setattr("shared.http_dial.get", get)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gateway")
    monkeypatch.setattr(
        "shared.machine.gateway_auth_headers", lambda: {"Authorization": "Bearer cluster"}
    )
    assert cmd_agents_timeline(405) == 0
    assert seen["url"] == "http://gateway/api/agents/405/timeline"
    assert seen["params"] == {"limit": 50}
    assert json.loads(capsys.readouterr().out) == payload


def test_request_uses_external_identity_and_returns_token_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def request(agent_id: int, **kwargs: Any) -> dict[str, Any]:
        seen.update({"agent_id": agent_id, **kwargs})
        return {
            "id": "lease",
            "token": "new-credential",
            "expires_at": datetime(2026, 9, 5, tzinfo=UTC),
        }

    monkeypatch.setattr(control, "request", request)
    assert (
        cli.cmd_impersonate(
            _args("request", "--agent", "405", "--as", "codex:task1", "--ttl", "600")
        )
        == 0
    )
    assert seen["caller"].source() == "external_agent:codex:task1"
    assert seen["ttl_seconds"] == 600
    assert json.loads(capsys.readouterr().out)["token"] == "new-credential"


def test_ack_uses_explicit_processed_ids_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, str, list[int]]] = []

    def ack(lease: str, token: str, ids: list[int]) -> None:
        seen.append((lease, token, ids))

    monkeypatch.setenv("AVA_IMPERSONATION_TOKEN", "credential")
    monkeypatch.setattr(control, "ack", ack)
    assert cli.cmd_impersonate(_args("ack", "lease", "11", "13")) == 0
    assert seen == [("lease", "credential", [11, 13])]
    assert json.loads(capsys.readouterr().out) == {"acknowledged": [11, 13]}


def test_missing_credential_fails_without_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("AVA_IMPERSONATION_TOKEN", raising=False)
    assert cli.cmd_impersonate(_args("status", "lease")) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "AVA_IMPERSONATION_TOKEN" in output.err


def test_release_preserves_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def release(_lease: str, _token: str, summary: str) -> dict[str, Any]:
        seen.append(summary)
        return {"status": "released"}

    monkeypatch.setenv("AVA_IMPERSONATION_TOKEN", "credential")
    monkeypatch.setattr(control, "release", release)
    assert cli.cmd_impersonate(_args("release", "lease", "--summary", "Completed X.\nNext Y.")) == 0
    assert seen == ["Completed X.\nNext Y."]


@pytest.mark.parametrize("remote", [None, "unix:///tmp/ava-codex.sock"])
def test_relay_parser(remote: str | None) -> None:
    args = _args(
        "relay",
        "405",
        "--lease-id",
        "lease",
        "--provider",
        "codex",
        "--thread-id",
        "thread",
        *(["--codex-remote", remote] if remote is not None else []),
    )
    assert args.provider == "codex"
    assert args.thread_id == "thread"
    assert args.func.__name__ == "_h_impersonate_relay"
    assert args.codex_remote == remote


@pytest.mark.parametrize(
    ("command", "option", "invalid_values"),
    [
        (["request", "--agent", "405", "--as", "codex"], "--ttl", ["0", "86401"]),
        (["renew", "lease"], "--ttl", ["-1", "86401"]),
        (["inbox", "lease"], "--limit", ["0", "1001"]),
        (["inbox", "lease"], "--wait", ["-1", "nan", "inf"]),
        (
            ["relay", "405", "--lease-id", "lease", "--provider", "claude"],
            "--debounce",
            ["-1", "30.1", "nan", "inf"],
        ),
    ],
)
def test_numeric_options_reject_out_of_bounds_values_during_parsing(
    command: list[str],
    option: str,
    invalid_values: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    for value in invalid_values:
        with pytest.raises(SystemExit) as raised:
            _args(*command, option, value)
        assert raised.value.code == 2
        output = capsys.readouterr()
        assert output.out == ""
        assert f"argument {option}:" in output.err
        assert "Traceback" not in output.err


@pytest.mark.parametrize(
    ("command", "option", "values"),
    [
        (["request", "--agent", "405", "--as", "codex"], "--ttl", ["1", "86400"]),
        (["renew", "lease"], "--ttl", ["1", "86400"]),
        (["inbox", "lease"], "--limit", ["1", "1000"]),
        (["inbox", "lease"], "--wait", ["0", "0.5", "86400"]),
        (
            ["relay", "405", "--lease-id", "lease", "--provider", "claude"],
            "--debounce",
            ["0", "0.5", "30"],
        ),
    ],
)
def test_numeric_options_accept_runtime_boundaries(
    command: list[str], option: str, values: list[str]
) -> None:
    for value in values:
        parsed = _args(*command, option, value)
        assert getattr(parsed, option.removeprefix("--")) == float(value)
