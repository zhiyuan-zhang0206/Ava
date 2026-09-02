"""`ava pitr snapshot` parser contracts."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import main as _main
from cli.commands import pitr as pitr_commands
from cli.parsers import pitr


@pytest.mark.parametrize(
    ("action", "handler_name"),
    (
        ("archive", "_h_pitr_snapshot_archive"),
        ("verify", "_h_pitr_snapshot_verify"),
        ("retire", "_h_pitr_snapshot_retire"),
    ),
)
def test_pitr_snapshot_actions_bind_their_dedicated_handlers(
    action: str, handler_name: str
) -> None:
    args = _main._build_parser().parse_args(
        ["pitr", "snapshot", action, "agent_state_backfill_snapshot"]
    )

    assert args.table == "agent_state_backfill_snapshot"
    assert args.func is getattr(_main, handler_name)


@pytest.mark.parametrize(
    ("handler", "command"),
    (
        (pitr._h_pitr_snapshot_archive, "cmd_pitr_snapshot_archive"),
        (pitr._h_pitr_snapshot_verify, "cmd_pitr_snapshot_verify"),
        (pitr._h_pitr_snapshot_retire, "cmd_pitr_snapshot_retire"),
    ),
)
def test_pitr_snapshot_handler_forwards_its_table(
    handler: Callable[[argparse.Namespace], int], command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli import commands

    tables: list[str] = []

    def fake(table: str) -> int:
        tables.append(table)
        return 17

    monkeypatch.setattr(commands, command, fake)  # pyright: ignore[reportUnknownArgumentType]

    assert handler(argparse.Namespace(table="agent_state_backfill_snapshot")) == 17
    assert tables == ["agent_state_backfill_snapshot"]


@pytest.mark.parametrize(
    ("command", "dependency", "action"),
    (
        (pitr_commands.cmd_pitr_snapshot_archive, "archive_rollback_snapshot", "archive"),
        (pitr_commands.cmd_pitr_snapshot_verify, "verify_rollback_snapshot", "verify"),
        (pitr_commands.cmd_pitr_snapshot_retire, "retire_rollback_snapshot", "retire"),
    ),
)
def test_pitr_snapshot_commands_report_failures_as_one_line_errors(
    command: Callable[[str], int],
    dependency: str,
    action: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated failure")

    class StoreGroup:
        def object_store(self) -> object:
            return object()

        def generation_pinned_object_reader(self) -> object:
            return object()

    monkeypatch.setattr(pitr_commands, dependency, fail)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(pitr_commands, "ava_home", lambda: tmp_path)
    monkeypatch.setattr(pitr_commands, "_pitr_backup_key", lambda: b"k" * 32)
    monkeypatch.setattr(pitr_commands, "get_store_group", StoreGroup)
    monkeypatch.setattr(
        pitr_commands,
        "settings",
        SimpleNamespace(physical_backup=SimpleNamespace(pitr_backup_key_id="archive-key-v1")),
    )

    assert command("agent_state_backfill_snapshot") == 1
    assert capsys.readouterr().err == f"pitr snapshot {action} failed: simulated failure\n"
