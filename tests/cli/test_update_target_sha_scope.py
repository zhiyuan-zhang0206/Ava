"""`ava cluster update --target-sha` no longer exists (issue #216).

The CLI is a thin POST client to the gateway; the gateway's rollout contract
has no pin parameter (the orchestration resolves origin/main once and Phase B
threads that single commit to each runner over the OPS channel, not via a CLI
flag). The flag was removed rather than refused: argparse rejects it outright,
so there is no silent-ignore window — a missing flag errors, and an unknown
flag errors too.

These tests pin the removal: argparse refuses the flag on every host, and
`cmd_update` carries no target_sha parameter (so a future re-add is a
deliberate contract change, not a silent resurrection). The hidden
`--rollout-log` flag remains because the detached gateway rollout uses it to
identify its own log; it is orchestration metadata, not a target pin.
"""

from __future__ import annotations

import pytest

from cli import main as _main


def test_target_sha_flag_rejected_by_argparse() -> None:
    """`ava cluster update --target-sha <sha>` is an unknown argument now —
    exit 2 with a usage error naming the flag, on every host."""
    with pytest.raises(SystemExit) as exc:
        _main.main(["cluster", "update", "--target-sha", "0123456789abcdef"])
    assert exc.value.code == 2


def test_target_sha_flag_rejected_even_with_local() -> None:
    """The `--local` escape hatch does not resurrect the pin — the in-process
    orchestration resolves its own target like the detached one."""
    with pytest.raises(SystemExit) as exc:
        _main.main(["cluster", "update", "--local", "--target-sha", "0123456789abcdef"])
    assert exc.value.code == 2


def test_cmd_update_has_only_the_internal_rollout_metadata_parameter() -> None:
    """The dispatch surface is pin-free. Phase B threads the resolved commit to
    runners over the ops channel (`spawn_update` -> `_run_agent_runner_self_update`
    with target_sha), never through this CLI verb."""
    import inspect

    from cli.commands import cmd_update

    assert "target_sha" not in inspect.signature(cmd_update).parameters
    assert "rollout_log" in inspect.signature(cmd_update).parameters


def test_rollout_log_flag_is_internal_but_parseable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _main._build_parser()
    log_path = "/home/ava/.ava/logs/rollout-1785470000.log"

    args = parser.parse_args(["cluster", "update", "--rollout-log", log_path])

    assert args.rollout_log == log_path
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["cluster", "update", "--help"])
    assert exc.value.code == 0
    assert "--rollout-log" not in capsys.readouterr().out


def test_rollout_log_flag_reaches_command_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import cli.commands as _commands

    seen: dict[str, object] = {}
    log_path = "/home/ava/.ava/logs/rollout-1785470000.log"

    def _fake(**kw: object) -> int:
        seen.update(kw)
        return 0

    monkeypatch.setattr(_commands, "cmd_update", _fake)

    assert _main.main(["cluster", "update", "--local", "--rollout-log", log_path]) == 0
    assert seen["rollout_log"] == log_path


def test_plain_update_still_dispatches() -> None:
    """A plain `ava cluster update` parses fine (no flags)."""
    import cli.main as _main_mod

    seen: list[dict[str, object]] = []
    monkeypatch_holder: list[object] = []

    # Patch cmd_update through cli.commands and drive main() with a real parse
    import cli.commands as _commands

    original = _commands.cmd_update

    def _fake(**kw: object) -> int:
        seen.append(kw)
        return 0

    _commands.cmd_update = _fake  # type: ignore[assignment]
    monkeypatch_holder.append(original)
    try:
        assert _main_mod.main(["cluster", "update"]) == 0
    finally:
        _commands.cmd_update = original  # type: ignore[assignment]
    assert seen and "target_sha" not in seen[0]
