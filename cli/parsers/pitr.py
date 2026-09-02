"""`ava pitr` inspection and rollback-snapshot archive parser."""

from __future__ import annotations

import argparse


def _h_pitr_retention_inspect(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_pitr_retention_inspect

    return cmd_pitr_retention_inspect()


def _h_pitr_snapshot_archive(args: argparse.Namespace) -> int:
    from cli.commands import cmd_pitr_snapshot_archive

    return cmd_pitr_snapshot_archive(args.table)


def _h_pitr_snapshot_verify(args: argparse.Namespace) -> int:
    from cli.commands import cmd_pitr_snapshot_verify

    return cmd_pitr_snapshot_verify(args.table)


def _h_pitr_snapshot_retire(args: argparse.Namespace) -> int:
    from cli.commands import cmd_pitr_snapshot_retire

    return cmd_pitr_snapshot_retire(args.table)


def _add_pitr_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import (
        _h_pitr_retention_inspect,
        _h_pitr_snapshot_archive,
        _h_pitr_snapshot_retire,
        _h_pitr_snapshot_verify,
    )

    pitr = sub.add_parser("pitr", help="inspect PITR evidence and archive rollback snapshots")
    pitr_sub = pitr.add_subparsers(dest="pitr_cmd", required=True)
    retention = pitr_sub.add_parser("retention", help="inspect retention dry-run plans")
    retention_sub = retention.add_subparsers(dest="retention_cmd", required=True)
    inspect = retention_sub.add_parser("inspect", help="show the latest local dry-run plan")
    inspect.set_defaults(func=_h_pitr_retention_inspect)
    snapshot = pitr_sub.add_parser("snapshot", help="archive finite migration rollback snapshots")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_cmd", required=True)
    for name, handler, help_text in (
        ("archive", _h_pitr_snapshot_archive, "export, encrypt, and upload a snapshot"),
        (
            "verify",
            _h_pitr_snapshot_verify,
            "restore an archived snapshot into disposable PostgreSQL",
        ),
        (
            "retire",
            _h_pitr_snapshot_retire,
            "drop a snapshot after successful archive verification",
        ),
    ):
        action = snapshot_sub.add_parser(name, help=help_text)
        action.add_argument("table", metavar="TABLE", help="rollback snapshot table name")
        action.set_defaults(func=handler)
