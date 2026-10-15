"""Thin CLI aliases and explicit controller operations."""

# ruff: noqa: S105 — fixture-only credential

from __future__ import annotations

import json
import sys
from argparse import Namespace
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import base.agents.impersonation.host_transport as impersonation_host_transport
import cli.commands.agents.control as agents_control
import cli.commands.agents.timeline as agents_timeline
from base.agents import impersonation as control
from base.agents.context.clients import DatabaseFactory
from base.agents.impersonation import history
from base.agents.impersonation import sessions as sessions
from cli.commands.agents import impersonation as cli
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.parsers import build_parser
from tests.path_scoped.cli_tests import operator_database as operator_database


def _private_id(_db: object, agent_id: int, session_id: int, *, local_machine: str) -> str:
    assert (agent_id, session_id) == (405, 0)
    return "lease"


def _public_session(value: dict[str, Any]) -> dict[str, Any]:
    return value


def _args(*args: str, command_runtime: CommandRuntime) -> Namespace:
    parsed = build_parser().parse_args(["impersonate", *args])
    parsed.runtime = command_runtime
    return parsed


def test_timeline_command_dispatches_to_the_timeline_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, str | None]] = []

    def timeline(agent_id: int, limit: int, before: str | None) -> int:
        calls.append((agent_id, limit, before))
        return 0

    monkeypatch.setattr(agents_timeline, "cmd_agents_timeline", timeline)
    args = build_parser().parse_args(
        ["agents", "timeline", "405", "--limit", "100", "--before", "12.0"]
    )
    assert args.func(args) == 0
    assert calls == [(405, 100, "12.0")]


def test_request_uses_external_identity_without_delivering_a_credential(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    seen: dict[str, Any] = {}

    def request(_db: object, _bus: object, agent_id: int, **kwargs: Any) -> dict[str, Any]:
        seen.update({"agent_id": agent_id, **kwargs})
        return {
            "id": "lease",
            "expires_at": datetime(2026, 9, 5, tzinfo=UTC),
        }

    monkeypatch.setattr(
        impersonation_host_transport,
        "default_control_endpoint",
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
                command_runtime=command_runtime,
            ),
            database_factory=operator_database,
        )
        == 0
    )
    assert seen["executor_name"] == "Codex: task1"
    assert seen["name"] == "Fix login"
    assert seen["process_metadata"]["pid"] > 0
    assert seen["process_metadata"]["invoked_python"] == sys.executable
    assert seen["ttl_seconds"] == 600
    assert seen["provider"] == "codex"
    assert seen["thread_id"] == "thread-1"
    assert seen["codex_remote"] == "unix:///tmp/codex.sock"
    assert seen["batch_window_seconds"] == 0
    output = capsys.readouterr()
    assert "token" not in json.loads(output.out)
    assert "starts the codex relay automatically" in output.err


def test_request_without_steer_endpoint_does_not_acquire_a_lease(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    from unittest.mock import Mock

    from base.agents.impersonation import host_transport

    request = Mock(return_value={})
    monkeypatch.setattr(sessions, "request", request)
    monkeypatch.setattr(host_transport, "default_control_endpoint", lambda: None)
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
        command_runtime=command_runtime,
    )
    assert cli.cmd_impersonate(args, database_factory=operator_database) == 1
    request.assert_not_called()
    assert "Steer" in capsys.readouterr().err


def test_request_records_the_shared_app_server_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    seen: dict[str, Any] = {}

    def request(_db: object, _bus: object, agent_id: int, **kwargs: Any) -> dict[str, Any]:
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
                command_runtime=command_runtime,
            ),
            database_factory=operator_database,
        )
        == 0
    )
    assert seen["codex_remote"] == endpoint
    assert seen["thread_id"] == "thread-1"
    assert "pass --codex-remote" in capsys.readouterr().err


def test_claude_request_reports_the_relay_handoff(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:

    def request(_db: object, _bus: object, agent_id: int, **kwargs: Any) -> dict[str, Any]:
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
                command_runtime=command_runtime,
            ),
            database_factory=operator_database,
        )
        == 0
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["relay_token"] == "relay-token"
    assert "AVA_IMPERSONATION_RELAY_TOKEN" in output.err
    assert "relay-token" not in output.err


def test_ack_uses_explicit_processed_ids_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    seen: list[tuple[str, list[int]]] = []

    def ack(
        _db: object,
        _bus: object,
        lease: str,
        ids: list[int],
        *,
        local_machine: str,
    ) -> None:
        assert local_machine == command_runtime.machine.name()
        seen.append((lease, ids))

    monkeypatch.setattr(
        sessions,
        "private_id",
        _private_id,
    )
    monkeypatch.setattr(control, "ack", ack)
    assert (
        cli.cmd_impersonate(
            _args("ack", "0", "11", "13", "--agent", "405", command_runtime=command_runtime),
            database_factory=operator_database,
        )
        == 0
    )
    assert seen[0][0] == "lease"
    assert seen[0][1] == [11, 13]
    assert json.loads(capsys.readouterr().out) == {"acknowledged": [11, 13]}


def test_classified_attestation_refusal_fails_without_leaking_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    from base.agents.impersonation import ImpersonationError

    def deny(_db: object, _bus: object, _lease: str, *, local_machine: str) -> dict[str, Any]:
        raise ImpersonationError("Controller caller check failed (chain-mismatch): see docs")

    monkeypatch.setattr(sessions, "private_id", _private_id)
    monkeypatch.setattr(control, "get", deny)
    assert (
        cli.cmd_impersonate(
            _args("status", "0", "--agent", "405", command_runtime=command_runtime),
            database_factory=operator_database,
        )
        == 1
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "chain-mismatch" in output.err


def test_release_preserves_summary(
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    seen: list[str] = []

    def release(
        _db: object, _bus: object, _lease: str, summary: str, *, local_machine: str
    ) -> dict[str, Any]:
        assert local_machine == command_runtime.machine.name()
        seen.append(summary)
        return {"status": "released"}

    monkeypatch.setattr(
        sessions,
        "private_id",
        _private_id,
    )
    monkeypatch.setattr(control, "release", release)
    monkeypatch.setattr(history, "public_session", _public_session)
    assert (
        cli.cmd_impersonate(
            _args(
                "release",
                "0",
                "--agent",
                "405",
                "--summary",
                "Completed X.\nNext Y.",
                command_runtime=command_runtime,
            ),
            database_factory=operator_database,
        )
        == 0
    )
    assert seen == ["Completed X.\nNext Y."]


def test_request_relay_spec_is_checked_at_the_cli_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    """`_relay_spec_problem` mirrors the shared validator before dispatch."""
    monkeypatch.delenv("ANTIGRAVITY_CONVERSATION_ID", raising=False)
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
    codex_without_thread = _args(*base, "--provider", "codex", command_runtime=command_runtime)
    assert codex_without_thread.func(codex_without_thread) == 2
    assert "needs --thread-id" in capsys.readouterr().err

    ag_without_thread = _args(*base, "--provider", "antigravity", command_runtime=command_runtime)
    assert ag_without_thread.func(ag_without_thread) == 2
    assert "ANTIGRAVITY_CONVERSATION_ID" in capsys.readouterr().err

    bad_remote = _args(
        *base,
        "--provider",
        "codex",
        "--thread-id",
        "t",
        "--codex-remote",
        "http://bad",
        command_runtime=command_runtime,
    )
    assert bad_remote.func(bad_remote) == 2
    assert "unix:// or ws://" in capsys.readouterr().err

    claude_with_thread = _args(
        *base, "--provider", "claude", "--thread-id", "t", command_runtime=command_runtime
    )
    assert claude_with_thread.func(claude_with_thread) == 2
    assert "routes to its owner" in capsys.readouterr().err
    dsh_with_remote = _args(
        *base,
        "--provider",
        "dsh",
        "--codex-remote",
        "unix:///tmp/x.sock",
        command_runtime=command_runtime,
    )
    assert dsh_with_remote.func(dsh_with_remote) == 2
    assert "the dsh relay routes to its owner" in capsys.readouterr().err


def test_relay_spec_is_checked_at_the_cli_boundary(
    capsys: pytest.CaptureFixture[str], *, command_runtime: CommandRuntime
) -> None:
    codex_without_thread = _args(
        "relay",
        "405",
        "--lease-id",
        "lease",
        "--provider",
        "codex",
        command_runtime=command_runtime,
    )
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
        command_runtime=command_runtime,
    )
    assert claude_with_remote.func(claude_with_remote) == 2
    assert "routes to its owner" in capsys.readouterr().err


# -- impersonate send: the CLI form of speaking as the leased agent --


def test_send_delivers_as_the_borrowed_agent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    operator_database: DatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    seen: list[tuple[int, str, str]] = []

    def deliver(agent_id: int, content: str, *, source: str) -> str:
        seen.append((agent_id, content, source))
        return "enqueued"

    def require_active(_db: object, lease: str, *, local_machine: str) -> dict[str, Any]:
        assert lease == "lease"
        return {"agent_id": 405}

    monkeypatch.setattr(sessions, "private_id", _private_id)
    monkeypatch.setattr(control, "require_active", require_active)
    monkeypatch.setattr(agents_control, "send_agent_message", deliver)
    args = _args(
        "send",
        "0",
        "--agent",
        "405",
        "--to",
        "42",
        "--content",
        "hi",
        command_runtime=command_runtime,
    )
    args.database_factory = operator_database
    assert args.func(args) == 0
    assert seen == [(42, "hi", "agent:405")]
    assert json.loads(capsys.readouterr().out) == {
        "status": "enqueued",
        "to": 42,
        "source": "agent:405",
    }


@pytest.mark.parametrize("remote", [None, "unix:///tmp/ava-codex.sock"])
def test_relay_parser(remote: str | None, *, command_runtime: CommandRuntime) -> None:
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
        command_runtime=command_runtime,
    )
    assert args.provider == "codex"
    assert args.thread_id == "thread"
    assert args.func.__name__ == "_h_impersonate_relay"
    assert args.codex_remote == remote
    assert args.token_stdin is False
    with_stdin = _args(
        "relay",
        "405",
        "--lease-id",
        "lease",
        "--provider",
        "claude",
        "--token-stdin",
        command_runtime=command_runtime,
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
    *,
    command_runtime: CommandRuntime,
) -> None:
    for value in invalid_values:
        with pytest.raises(SystemExit) as raised:
            _args(*command, option, value, command_runtime=command_runtime)
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
    command: list[str], option: str, values: list[str], *, command_runtime: CommandRuntime
) -> None:
    for value in values:
        parsed = _args(*command, option, value, command_runtime=command_runtime)
        assert getattr(parsed, option.removeprefix("--")) == float(value)


def test_dsh_request_writes_the_plugin_stub_and_never_prints_the_credential(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operator_database: Callable[[], Any],
    *,
    command_runtime: CommandRuntime,
) -> None:
    from unittest.mock import Mock

    request = Mock(return_value={"id": "lease", "session_id": 9, "relay_token": "tok-9"})
    monkeypatch.setattr(sessions, "request", request)
    monkeypatch.delenv("DSH_AVA_RELAY_STUB", raising=False)
    argv = ("request", "--name", "Fix", "--agent", "405", "--as", "dsh", "--ttl", "600")
    args = (*argv, "--provider", "dsh", "--batch-window", "0")
    assert (
        cli.cmd_impersonate(
            _args(*args, command_runtime=command_runtime), database_factory=operator_database
        )
        == 1
    )  # no plugin: refused before any lease
    request.assert_not_called()
    assert "DSH_AVA_RELAY_STUB is unset" in capsys.readouterr().err
    stub = tmp_path / "session-1.env"
    monkeypatch.setenv("DSH_AVA_RELAY_STUB", str(stub))
    assert (
        cli.cmd_impersonate(
            _args(*args, command_runtime=command_runtime), database_factory=operator_database
        )
        == 0
    )
    output = capsys.readouterr()
    assert "relay_token" not in json.loads(output.out) and "tok-9" not in output.out + output.err
    assert stub.read_text() == (
        "SID=9\nAGENT=405\nAVA_IMPERSONATION_RELAY_TOKEN=tok-9\n"
        f"AVA_IMPERSONATION_RELAY_PY={cli.sys.executable}\n"
    )
    assert (stub.stat().st_mode & 0o777) == 0o600
