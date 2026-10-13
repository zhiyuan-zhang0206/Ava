"""Parser-only impersonation usage keeps required CLI inputs explicit."""

from argparse import Namespace

import pytest

from cli.parsers import build_parser


def _args(*args: str) -> Namespace:
    return build_parser().parse_args(["impersonate", *args])


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
