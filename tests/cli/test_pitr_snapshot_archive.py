"""`ava pitr snapshot` parser contracts."""

from __future__ import annotations

import argparse
from collections.abc import Callable

import pytest

from cli import main as _main
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
