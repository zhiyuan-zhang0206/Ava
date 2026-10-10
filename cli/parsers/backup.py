"""`ava backup` parser: WAL-G physical backup commands."""

from __future__ import annotations

import argparse


def _h_backup_walg_check(_args: argparse.Namespace) -> int:
    from cli.commands.data_plane.walg import cmd_walg_check

    return cmd_walg_check()


def _h_backup_walg_run(_args: argparse.Namespace) -> int:
    from cli.commands.data_plane.walg import cmd_walg_run

    return cmd_walg_run(
        database_factory=_args.database_factory, database_for_url=_args.database_for_url
    )


def _h_backup_walg_drill(_args: argparse.Namespace) -> int:
    from cli.commands.data_plane.walg import cmd_walg_drill

    return cmd_walg_drill(database_for_url=_args.database_for_url)


def _h_backup_walg_restore(args: argparse.Namespace) -> int:
    from cli.commands.data_plane.walg import cmd_walg_restore

    return cmd_walg_restore(
        directory=args.dir, backup=args.backup, time=args.time, lsn=args.lsn, user=args.user
    )


def _h_backup_walg_status(_args: argparse.Namespace) -> int:
    from cli.commands.data_plane.walg import cmd_walg_status

    return cmd_walg_status()


def _add_backup_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    backup = sub.add_parser("backup", help="WAL-G physical backup commands")
    backup_sub = backup.add_subparsers(dest="backup_cmd", required=True)
    walg = backup_sub.add_parser(
        "walg", help="WAL-G physical backup: pre-flight check, daily run, drill, restore, status"
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
    walg_drill = walg_sub.add_parser(
        "drill",
        help="restore the newest backup into a scratch Postgres, recover it to the newest "
        "archived WAL and read a real conversation back (the daily run does this weekly)",
    )
    walg_drill.set_defaults(func=_h_backup_walg_drill)
    walg_restore = walg_sub.add_parser(
        "restore",
        help="restore a backup into an empty directory and recover it to a target time or "
        "LSN with a scratch Postgres (never touches this home's data directory)",
    )
    walg_restore.add_argument("--dir", required=True, help="an empty or missing directory")
    walg_restore.add_argument(
        "--backup",
        default="LATEST",
        help="backup name (an increment resolves its chain); default LATEST",
    )
    walg_restore.add_argument(
        "--user",
        help="the restored cluster's superuser: the OS user that ran initdb on the source "
        "(default: the current OS user)",
    )
    target = walg_restore.add_mutually_exclusive_group()
    target.add_argument("--time", help="recover to this time (UTC), e.g. '2026-10-01 12:04:57+00'")
    target.add_argument("--lsn", help="recover to this LSN, e.g. 0/3000060")
    walg_restore.set_defaults(func=_h_backup_walg_restore)
    walg_status = walg_sub.add_parser(
        "status", help="show the WAL-G configuration, key fingerprint and archiver state"
    )
    walg_status.set_defaults(func=_h_backup_walg_status)
