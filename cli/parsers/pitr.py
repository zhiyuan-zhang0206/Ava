"""`ava pitr` read-only inspection parser."""

from __future__ import annotations

import argparse


def _h_pitr_retention_inspect(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_pitr_retention_inspect

    return cmd_pitr_retention_inspect()


def _add_pitr_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_pitr_retention_inspect

    pitr = sub.add_parser("pitr", help="inspect physical backup evidence")
    pitr_sub = pitr.add_subparsers(dest="pitr_cmd", required=True)
    retention = pitr_sub.add_parser("retention", help="inspect retention dry-run plans")
    retention_sub = retention.add_subparsers(dest="retention_cmd", required=True)
    inspect = retention_sub.add_parser("inspect", help="show the latest local dry-run plan")
    inspect.set_defaults(func=_h_pitr_retention_inspect)
