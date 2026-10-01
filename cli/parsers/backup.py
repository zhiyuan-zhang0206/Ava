"""`ava backup` parser: custody of the scheduled backup operations."""

from __future__ import annotations

import argparse


def _h_backup_operations_status(_args: argparse.Namespace) -> int:
    from cli.commands.data_plane.backup_operations import cmd_backup_operations_status

    return cmd_backup_operations_status()


def _h_backup_operations_retire(args: argparse.Namespace) -> int:
    from cli.commands.data_plane.backup_operations import cmd_backup_operations_retire

    return cmd_backup_operations_retire(confirm=args.confirm)


def _add_backup_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    backup = sub.add_parser("backup", help="inspect the scheduled backup operations")
    backup_sub = backup.add_subparsers(dest="backup_cmd", required=True)
    operations = backup_sub.add_parser(
        "operations",
        help="inspect backup operation custody and retire blocked operations",
    )
    operations_sub = operations.add_subparsers(dest="operations_cmd", required=True)
    status = operations_sub.add_parser(
        "status", help="show blocked operation kinds and quarantined operations"
    )
    status.set_defaults(func=_h_backup_operations_status)
    retire = operations_sub.add_parser(
        "retire",
        help="re-prove group closure of blocked operations, then quarantine them",
    )
    retire.add_argument(
        "--confirm",
        action="store_true",
        help="quarantine every proven operation; without it the command only previews",
    )
    retire.set_defaults(func=_h_backup_operations_retire)
