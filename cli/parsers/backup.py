"""`ava backup` parser: custody of the scheduled backup operations and the WAL-G physical backup."""

from __future__ import annotations

import argparse


def _h_backup_operations_status(_args: argparse.Namespace) -> int:
    from cli.commands.data_plane.backup_operations import cmd_backup_operations_status

    return cmd_backup_operations_status()


def _h_backup_operations_retire(args: argparse.Namespace) -> int:
    from cli.commands.data_plane.backup_operations import cmd_backup_operations_retire

    return cmd_backup_operations_retire(confirm=args.confirm)


def _h_backup_walg_check(_args: argparse.Namespace) -> int:
    from cli.commands.data_plane.walg import cmd_walg_check

    return cmd_walg_check()


def _h_backup_walg_run(_args: argparse.Namespace) -> int:
    from cli.commands.data_plane.walg import cmd_walg_run

    return cmd_walg_run()


def _h_backup_walg_status(_args: argparse.Namespace) -> int:
    from cli.commands.data_plane.walg import cmd_walg_status

    return cmd_walg_status()


def _add_backup_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    backup = sub.add_parser(
        "backup", help="inspect the scheduled backup operations and the WAL-G physical backup"
    )
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

    walg = backup_sub.add_parser(
        "walg", help="WAL-G physical backup: pre-flight check, daily run, status"
    )
    walg_sub = walg.add_subparsers(dest="walg_cmd", required=True)
    walg_check = walg_sub.add_parser(
        "check",
        help="prove the binary, configuration, key and storage permissions work before "
        "switching WAL archiving on (writes and deletes one small object under the prefix)",
    )
    walg_check.set_defaults(func=_h_backup_walg_check)
    walg_run = walg_sub.add_parser(
        "run",
        help="run one daily tick: backup, verify the archived WAL chain, apply retention "
        "(the scheduled job runs this; safe to repeat, concurrent runs stand down)",
    )
    walg_run.set_defaults(func=_h_backup_walg_run)
    walg_status = walg_sub.add_parser(
        "status", help="show the WAL-G configuration, key fingerprint and archiver state"
    )
    walg_status.set_defaults(func=_h_backup_walg_status)
