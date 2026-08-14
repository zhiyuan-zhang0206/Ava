"""`ava cluster update --target-sha` is an agent-runner-only flag.

The gateway orchestration resolves its own rollout pin once
(`_resolve_rollout_target` -> `origin/main`) and fans that single commit out to
every runner, so there is no second target it could honour. Before this guard the
flag was accepted and dropped on every gateway route — argparse took it,
`cmd_update` forwarded it only into `_run_agent_runner_self_update`, and a
gateway-capable host went to `_spawn_gateway_rollout` (default) or
`_run_gateway_orchestration` (`--local`), neither of which has the parameter. An
operator pinning a rollout that way got a live-resolved tip and no indication of
it. A missing flag errors; an ignored one misinforms.

These tests pin both halves: refusal on any gateway-capable host (pure gateway
and the single-box `gateway,agent-runner`), and the still-working agent-runner
thread-through that Phase B depends on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli import commands as _cli

_SHA = "0123456789abcdef0123456789abcdef01234567"


def _fail_if_called(what: str):
    def _boom(*_a: object, **_kw: object) -> int:
        raise AssertionError(f"{what} must not run when --target-sha is refused")

    return _boom


@pytest.mark.parametrize(
    "roles",
    [
        frozenset({"gateway"}),
        frozenset({"gateway", "agent-runner"}),  # the single box
    ],
    ids=["gateway", "single-box"],
)
@pytest.mark.parametrize(
    ("local", "restart_only"),
    [
        (False, False),  # default: would have spawned the detached rollout
        (True, False),  # would have run the in-process orchestration
        (False, True),  # in-process too; pins nothing at all
    ],
    ids=["default", "local", "restart-only"],
)
def test_target_sha_refused_on_gateway_capable_host(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    roles: frozenset[str],
    local: bool,
    restart_only: bool,
) -> None:
    """Every gateway route refuses --target-sha with exit 2 and runs nothing.

    Parameterized across all three dispatch routes because the flag was dropped
    by each of them independently — guarding only the default would leave
    `--local` (what the detached rollout session itself runs) still silent.
    """
    monkeypatch.setattr("shared.machine.machine_role", lambda: roles)
    monkeypatch.setattr(_cli, "_repo_root", lambda: Path("/repo"))
    monkeypatch.setattr(
        _cli, "_run_gateway_orchestration", _fail_if_called("the gateway orchestration")
    )
    monkeypatch.setattr(
        _cli, "_run_agent_runner_self_update", _fail_if_called("the agent-runner self-update")
    )
    monkeypatch.setattr("ops.cluster.spawn_rollout", _fail_if_called("spawn_rollout"))

    rc = _cli.cmd_update(target_sha=_SHA, local=local, restart_only=restart_only)

    assert rc == 2
    err = capsys.readouterr().err
    # The refusal must name the flag, and must say what to do instead — a bare
    # "not accepted" would leave the operator with no next move.
    assert "--target-sha" in err
    assert _SHA[:12] in err
    assert "ava cluster rollback --to" in err


def test_gateway_without_target_sha_still_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is scoped to the flag: a plain gateway `ava cluster update` is untouched."""
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"gateway"}))
    spawned: list[str] = []
    monkeypatch.setattr(
        "ops.cluster.spawn_rollout",
        lambda origin, **_kw: (  # pyright: ignore[reportUnknownArgumentType]
            spawned.append(origin) or {"session": "ava-rollout", "log": "logs/updater.log"}  # pyright: ignore[reportUnknownArgumentType]
        ),
    )

    assert _cli.cmd_update() == 0
    assert len(spawned) == 1


def test_target_sha_threads_through_on_pure_agent_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """The honest path is unchanged — a pure agent-runner still receives the pin.

    This is the Phase B contract: the gateway resolves one commit and hands it to
    each runner's `ava cluster update --local --target-sha <sha>`. Breaking it would break
    every multi-machine rollout, so the refusal above must not reach this host.
    """
    monkeypatch.setattr("shared.machine.machine_role", lambda: frozenset({"agent-runner"}))
    monkeypatch.setattr(_cli, "_repo_root", lambda: Path("/repo"))
    seen: list[str | None] = []
    monkeypatch.setattr(
        _cli,
        "_run_agent_runner_self_update",
        lambda _repo, *, target_sha, **_kw: seen.append(target_sha) or 0,  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _cli.cmd_update(target_sha=_SHA, local=True) == 0
    assert seen == [_SHA]


def test_cli_flag_reaches_cmd_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ava cluster update --target-sha <sha>` wires argparse -> cmd_update.

    Guards the seam the refusal depends on: if `cli/main.py` stopped forwarding
    the parsed value, `cmd_update` would see None and the gateway guard would
    never fire — the flag would be silently ignored again, with tests above
    still green because they call `cmd_update` directly.
    """
    import cli.main as _main

    seen: list[str | None] = []
    monkeypatch.setattr(
        "cli.commands.cmd_update",
        lambda **kw: seen.append(kw["target_sha"]) or 0,  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _main.main(["cluster", "update", "--target-sha", _SHA]) == 0
    assert seen == [_SHA]
