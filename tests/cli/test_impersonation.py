"""Thin CLI aliases and explicit controller operations."""

# ruff: noqa: S105 — fixture-only credential

from __future__ import annotations

import json
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from cli.commands import impersonation as cli
from cli.commands.agent_timeline import cmd_agents_timeline
from cli.parsers import build_parser
from shared import impersonation as control
from shared import impersonation_sessions as sessions


def _private_id(agent_id: int, session_id: int) -> str:
    assert (agent_id, session_id) == (405, 0)
    return "lease"


def _public_session(value: dict[str, Any]) -> dict[str, Any]:
    return value


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
    assert seen["params"] == {}  # no --limit: the configured gateway default applies
    assert json.loads(capsys.readouterr().out) == payload


def test_request_uses_external_identity_without_delivering_a_credential(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def request(agent_id: int, **kwargs: Any) -> dict[str, Any]:
        seen.update({"agent_id": agent_id, **kwargs})
        return {
            "id": "lease",
            "expires_at": datetime(2026, 9, 5, tzinfo=UTC),
        }

    monkeypatch.setattr(
        "cli.commands.codex_app_server.default_control_endpoint",
        lambda: "unix:///tmp/codex.sock",
    )
    monkeypatch.setattr(sessions, "request", request)
    assert (
        cli.cmd_impersonate(
            _args(
                "request",
                "--name",
                "Fix login",
                "--agent",
                "405",
                "--as",
                "Codex: task1",
                "--ttl",
                "600",
                "--provider",
                "codex",
                "--thread-id",
                "thread-1",
                "--batch-window",
                "0",
            )
        )
        == 0
    )
    assert seen["executor_name"] == "Codex: task1"
    assert seen["name"] == "Fix login"
    assert seen["process_metadata"]["pid"] > 0
    assert seen["ttl_seconds"] == 600
    assert seen["provider"] == "codex"
    assert seen["thread_id"] == "thread-1"
    assert seen["codex_remote"] == "unix:///tmp/codex.sock"
    assert seen["batch_window_seconds"] == 0
    output = capsys.readouterr()
    assert "token" not in json.loads(output.out)
    assert "starts the codex relay automatically" in output.err


def test_request_without_steer_endpoint_does_not_acquire_a_lease(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from unittest.mock import Mock

    from cli.commands import codex_app_server

    request = Mock(return_value={})
    monkeypatch.setattr(sessions, "request", request)
    monkeypatch.setattr(codex_app_server, "default_control_endpoint", lambda: None)
    args = _args(
        "request",
        "--name",
        "Steer test",
        "--agent",
        "405",
        "--as",
        "Codex",
        "--ttl",
        "600",
        "--provider",
        "codex",
        "--thread-id",
        "thread-1",
        "--batch-window",
        "0",
    )
    assert cli.cmd_impersonate(args) == 1
    request.assert_not_called()
    assert "Steer" in capsys.readouterr().err


def test_request_records_the_shared_app_server_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: dict[str, Any] = {}

    def request(agent_id: int, **kwargs: Any) -> dict[str, Any]:
        seen.update({"agent_id": agent_id, **kwargs})
        return {"id": "lease", "expires_at": datetime(2026, 9, 5, tzinfo=UTC)}

    monkeypatch.setattr(sessions, "request", request)
    endpoint = "unix:///home/u/.ava-lc/run/codex-app-server.0123456789ab-01234567.sock"
    assert (
        cli.cmd_impersonate(
            _args(
                "request",
                "--name",
                "Fix login",
                "--agent",
                "405",
                "--as",
                "Codex: task1",
                "--ttl",
                "600",
                "--provider",
                "codex",
                "--thread-id",
                "thread-1",
                "--codex-remote",
                endpoint,
                "--batch-window",
                "0",
            )
        )
        == 0
    )
    assert seen["codex_remote"] == endpoint
    assert seen["thread_id"] == "thread-1"
    assert "pass --codex-remote" in capsys.readouterr().err


def test_claude_request_reports_the_relay_handoff(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def request(agent_id: int, **kwargs: Any) -> dict[str, Any]:
        return {"id": "lease", "relay_token": "relay-token"}

    monkeypatch.setattr(sessions, "request", request)
    assert (
        cli.cmd_impersonate(
            _args(
                "request",
                "--name",
                "Fix login",
                "--agent",
                "405",
                "--as",
                "claude:task1",
                "--ttl",
                "600",
                "--provider",
                "claude",
                "--batch-window",
                "0",
            )
        )
        == 0
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["relay_token"] == "relay-token"
    assert "AVA_IMPERSONATION_RELAY_TOKEN" in output.err
    assert "relay-token" not in output.err


def test_request_requires_a_relay_provider(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --provider is mandatory at parse time; the codex thread requirement is
    # enforced at the CLI boundary (`_relay_spec_problem`, covered below).
    with pytest.raises(SystemExit) as raised:
        _args(
            "request",
            "--name",
            "Fix login",
            "--agent",
            "405",
            "--as",
            "codex",
            "--ttl",
            "600",
            "--batch-window",
            "0",
        )
    assert raised.value.code == 2
    assert "--provider" in capsys.readouterr().err


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


def test_ack_uses_explicit_processed_ids_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, dict[str, Any], list[int]]] = []

    def ack(lease: str, attesting: dict[str, Any], ids: list[int]) -> None:
        seen.append((lease, attesting, ids))

    monkeypatch.setattr(
        sessions,
        "private_id",
        _private_id,
    )
    monkeypatch.setattr(control, "ack", ack)
    assert cli.cmd_impersonate(_args("ack", "0", "11", "13", "--agent", "405")) == 0
    assert seen[0][0] == "lease"
    assert seen[0][1]["pid"] > 0
    assert seen[0][2] == [11, 13]
    assert json.loads(capsys.readouterr().out) == {"acknowledged": [11, 13]}


def test_classified_attestation_refusal_fails_without_leaking_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from shared.impersonation import ImpersonationError

    def deny(_lease: str, _caller: object) -> dict[str, Any]:
        raise ImpersonationError("Controller caller check failed (chain-mismatch): see docs")

    monkeypatch.setattr(sessions, "private_id", _private_id)
    monkeypatch.setattr(control, "get", deny)
    assert cli.cmd_impersonate(_args("status", "0", "--agent", "405")) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "chain-mismatch" in output.err


def test_release_preserves_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def release(_lease: str, _caller: object, summary: str) -> dict[str, Any]:
        seen.append(summary)
        return {"status": "released"}

    monkeypatch.setattr(
        sessions,
        "private_id",
        _private_id,
    )
    monkeypatch.setattr(control, "release", release)
    monkeypatch.setattr("shared.impersonation_history.public_session", _public_session)
    assert (
        cli.cmd_impersonate(
            _args("release", "0", "--agent", "405", "--summary", "Completed X.\nNext Y.")
        )
        == 0
    )
    assert seen == ["Completed X.\nNext Y."]


def test_batch_window_zero_disables_merge() -> None:
    """--batch-window 0 is the documented merge-off value; the CLI must reach
    the DB layer's 0..300 contract (issue #2056: the parser hardcoded min=1,
    so 0 was unreachable from the CLI)."""
    for value in ("0", "300"):
        args = _args(
            "request",
            "--name",
            "Fix login",
            "--agent",
            "405",
            "--as",
            "codex",
            "--ttl",
            "600",
            "--provider",
            "codex",
            "--thread-id",
            "t",
            "--batch-window",
            value,
        )
        assert args.relay_batch_window_seconds == int(value)


def test_request_without_batch_window_is_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explicit-parameter ruling: --batch-window has no default, so even the
    documented merge-off value (0 — see #2056) must be written out."""
    with pytest.raises(SystemExit) as raised:
        _args(
            "request",
            "--name",
            "Fix login",
            "--agent",
            "405",
            "--as",
            "codex",
            "--ttl",
            "600",
            "--provider",
            "codex",
            "--thread-id",
            "t",
        )
    assert raised.value.code == 2
    assert "--batch-window" in capsys.readouterr().err


def test_request_requires_explicit_ttl(capsys: pytest.CaptureFixture[str]) -> None:
    """Explicit-parameter ruling: the lease lifetime has no default."""
    with pytest.raises(SystemExit) as raised:
        _args(
            "request",
            "--name",
            "Fix login",
            "--agent",
            "405",
            "--as",
            "codex",
            "--provider",
            "codex",
            "--thread-id",
            "t",
            "--batch-window",
            "0",
        )
    assert raised.value.code == 2
    assert "--ttl" in capsys.readouterr().err


def test_renew_requires_ttl(capsys: pytest.CaptureFixture[str]) -> None:
    """Explicit-parameter ruling: renewal states the new lifetime outright —
    no keep-the-current-length default."""
    with pytest.raises(SystemExit) as raised:
        _args("renew", "0", "--agent", "405")
    assert raised.value.code == 2
    assert "--ttl" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--name", "--as"])
def test_request_display_fields_must_be_non_empty(
    option: str, capsys: pytest.CaptureFixture[str]
) -> None:
    command = [
        "request",
        "--name",
        "Fix login",
        "--agent",
        "405",
        "--as",
        "codex",
        "--ttl",
        "600",
        "--provider",
        "codex",
        "--thread-id",
        "t",
        "--batch-window",
        "0",
    ]
    command[command.index(option) + 1] = "   "
    with pytest.raises(SystemExit) as raised:
        _args(*command)
    assert raised.value.code == 2
    assert f"argument {option}:" in capsys.readouterr().err


def test_request_relay_spec_is_checked_at_the_cli_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`_relay_spec_problem` mirrors the shared validator before dispatch."""
    base = [
        "request",
        "--name",
        "Fix login",
        "--agent",
        "405",
        "--as",
        "codex",
        "--ttl",
        "600",
        "--batch-window",
        "0",
    ]
    codex_without_thread = _args(*base, "--provider", "codex")
    assert codex_without_thread.func(codex_without_thread) == 2
    assert "needs --thread-id" in capsys.readouterr().err

    bad_remote = _args(
        *base, "--provider", "codex", "--thread-id", "t", "--codex-remote", "http://bad"
    )
    assert bad_remote.func(bad_remote) == 2
    assert "unix:// or ws://" in capsys.readouterr().err

    claude_with_thread = _args(*base, "--provider", "claude", "--thread-id", "t")
    assert claude_with_thread.func(claude_with_thread) == 2
    assert "routes to its owner" in capsys.readouterr().err


def test_relay_spec_is_checked_at_the_cli_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    codex_without_thread = _args("relay", "405", "--lease-id", "lease", "--provider", "codex")
    assert codex_without_thread.func(codex_without_thread) == 2
    assert "needs --thread-id" in capsys.readouterr().err

    claude_with_remote = _args(
        "relay",
        "405",
        "--lease-id",
        "lease",
        "--provider",
        "claude",
        "--codex-remote",
        "unix:///tmp/x.sock",
    )
    assert claude_with_remote.func(claude_with_remote) == 2
    assert "routes to its owner" in capsys.readouterr().err


# -- impersonate send: the attested CLI form of speaking as the leased agent --


def test_send_requires_session_agent_target_and_content() -> None:
    for command in (
        ["--agent", "405", "--to", "42", "--content", "hi"],  # no session id
        ["0", "--to", "42", "--content", "hi"],  # no --agent
        ["0", "--agent", "405", "--content", "hi"],  # no --to
        ["0", "--agent", "405", "--to", "42"],  # no --content
    ):
        with pytest.raises(SystemExit) as raised:
            _args("send", *command)
        assert raised.value.code == 2


def test_send_delivers_as_the_borrowed_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[int, str, str]] = []

    def deliver(agent_id: int, content: str, *, source: str) -> str:
        seen.append((agent_id, content, source))
        return "enqueued"

    def require_active(lease: str, caller: dict[str, Any]) -> dict[str, Any]:
        assert lease == "lease"
        assert caller["pid"] > 0
        return {"agent_id": 405}

    monkeypatch.setattr(sessions, "private_id", _private_id)
    monkeypatch.setattr(control, "require_active", require_active)
    monkeypatch.setattr("cli.commands.agents.send_agent_message", deliver)
    args = _args("send", "0", "--agent", "405", "--to", "42", "--content", "hi")
    assert args.func(args) == 0
    assert seen == [(42, "hi", "agent:405")]
    assert json.loads(capsys.readouterr().out) == {
        "status": "enqueued",
        "to": 42,
        "source": "agent:405",
    }


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
    assert args.token_stdin is False
    with_stdin = _args(
        "relay", "405", "--lease-id", "lease", "--provider", "claude", "--token-stdin"
    )
    assert with_stdin.token_stdin is True


@pytest.mark.parametrize(
    ("command", "option", "invalid_values"),
    [
        (
            [
                "request",
                "--name",
                "Fix login",
                "--agent",
                "405",
                "--as",
                "codex",
                "--provider",
                "codex",
                "--thread-id",
                "t",
            ],
            "--ttl",
            ["0", "86401"],
        ),
        (["renew", "0", "--agent", "405"], "--ttl", ["-1", "86401"]),
        (["inbox", "0", "--agent", "405"], "--limit", ["0", "1001"]),
        (["inbox", "0", "--agent", "405"], "--wait", ["-1", "nan", "inf"]),
        (
            ["relay", "405", "--lease-id", "lease", "--provider", "claude"],
            "--debounce",
            ["-1", "30.1", "nan", "inf"],
        ),
        (
            [
                "request",
                "--name",
                "Fix login",
                "--agent",
                "405",
                "--as",
                "codex",
                "--provider",
                "codex",
                "--thread-id",
                "t",
            ],
            "--batch-window",
            ["-1", "301"],
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
        (
            [
                "request",
                "--name",
                "Fix login",
                "--agent",
                "405",
                "--as",
                "codex",
                "--provider",
                "codex",
                "--thread-id",
                "t",
                "--batch-window",
                "0",
            ],
            "--ttl",
            ["1", "86400"],
        ),
        (["renew", "0", "--agent", "405"], "--ttl", ["1", "86400"]),
        (["inbox", "0", "--agent", "405"], "--limit", ["1", "1000"]),
        (["inbox", "0", "--agent", "405"], "--wait", ["0", "0.5", "86400"]),
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


def test_request_writes_the_resident_stub_when_scoped(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def request(agent_id: int, **kwargs: Any) -> dict[str, Any]:
        return {
            "id": "lease",
            "session_id": 9,
            "relay_token": "tok-123",
            "status": "preparing",
        }

    stub = tmp_path / ".ava-relay.env"
    monkeypatch.setattr(sessions, "request", request)
    monkeypatch.setenv("AVA_IMPERSONATION_RELAY_STUB", str(stub))
    assert (
        cli.cmd_impersonate(
            _args(
                "request",
                "--name",
                "Fix login",
                "--agent",
                "405",
                "--as",
                "Claude: task1",
                "--ttl",
                "3600",
                "--provider",
                "claude",
                "--batch-window",
                "0",
            )
        )
        == 0
    )
    assert stub.read_text() == "SID=9\nAGENT=405\nAVA_IMPERSONATION_RELAY_TOKEN=tok-123\n"
    assert (stub.stat().st_mode & 0o777) == 0o600
    err = capsys.readouterr().err
    assert "session plugin starts the claude relay automatically" in err
    assert "arm it as a Monitor watch" in err
