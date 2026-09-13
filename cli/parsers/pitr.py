"""`ava pitr` inspection and rollback-snapshot archive parser."""

from __future__ import annotations

import argparse


def _h_pitr_drill(args: argparse.Namespace) -> int:
    from cli.commands import cmd_pitr_drill

    return cmd_pitr_drill(
        chain=args.chain,
        candidate=args.candidate,
        target_lsn=args.target_lsn,
        target_wall=args.target_wall,
        scratch=args.scratch,
        timeout_seconds=args.promotion_timeout,
    )


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
        _h_pitr_drill,
        _h_pitr_retention_inspect,
        _h_pitr_snapshot_archive,
        _h_pitr_snapshot_retire,
        _h_pitr_snapshot_verify,
    )

    pitr = sub.add_parser("pitr", help="inspect PITR evidence and archive rollback snapshots")
    pitr_sub = pitr.add_subparsers(dest="pitr_cmd", required=True)
    drill = pitr_sub.add_parser(
        "drill",
        help="restore a protected chain to an operator target in an isolated sandbox",
    )
    drill.add_argument(
        "--chain",
        metavar="CHAIN",
        help="chain id resolved under $AVA_HOME/physical-backup/base-manifests",
    )
    drill.add_argument(
        "--candidate",
        metavar="PATH",
        help="explicit candidate manifest JSON instead of --chain",
    )
    drill.add_argument(
        "--target-lsn",
        metavar="LSN",
        required=True,
        help="recovery target LSN, e.g. 26/A03520B0",
    )
    drill.add_argument(
        "--target-wall",
        metavar="TIMESTAMP",
        required=True,
        help="target wall clock with an explicit UTC offset, e.g. '2026-09-13 13:13:03+08'",
    )
    drill.add_argument(
        "--scratch",
        metavar="DIR",
        required=True,
        help="fresh scratch directory; kept as the evidence tree",
    )
    drill.add_argument(
        "--promotion-timeout",
        metavar="SECONDS",
        type=int,
        default=1800,
        help="bound for postmaster start and replay to the target",
    )
    drill.set_defaults(func=_h_pitr_drill)
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
