"""`ava pitr` inspection and rollback-snapshot archive parser."""

from __future__ import annotations

import argparse
from collections.abc import Callable


def _target_wall_arg(value: str) -> str:
    """Argparse type for `pitr drill --target-wall`.

    Requires an offset-carrying ISO-8601 timestamp; reuses the drill service's
    parser so the CLI boundary and the drill agree on one definition.
    """
    from services.pitr.restore_drill import DrillError, parse_target_wall

    try:
        parse_target_wall(value)
    except DrillError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def _h_pitr_drill(args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_drill

    return cmd_pitr_drill(
        chain=args.chain,
        candidate=args.candidate,
        target_lsn=args.target_lsn,
        target_wall=args.target_wall,
        scratch=args.scratch,
        timeout_seconds=args.promotion_timeout,
    )


def _h_pitr_multipart_list(args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_multipart_list

    return cmd_pitr_multipart_list(prefix=args.prefix, credentials_file=args.credentials_file)


def _h_pitr_multipart_abort(args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_multipart_abort

    return cmd_pitr_multipart_abort(
        key=args.key,
        upload_id=args.upload_id,
        credentials_file=args.credentials_file,
        confirm=args.confirm,
    )


def _h_pitr_operations_status(_args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_operations_status

    return cmd_pitr_operations_status()


def _h_pitr_operations_retire(args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_operations_retire

    return cmd_pitr_operations_retire(confirm=args.confirm)


def _h_pitr_operations_discard_candidate(args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_operations_discard_candidate

    return cmd_pitr_operations_discard_candidate(chain=args.chain, confirm=args.confirm)


def _h_pitr_retention_inspect(_args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_retention_inspect

    return cmd_pitr_retention_inspect()


def _h_pitr_retention_arm(args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_retention_arm

    return cmd_pitr_retention_arm(digest=args.digest, confirm=args.confirm)


def _h_pitr_retention_disable(args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_retention_disable

    return cmd_pitr_retention_disable(confirm=args.confirm)


def _h_pitr_retention_status(_args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_retention_status

    return cmd_pitr_retention_status()


def _h_pitr_retention_run_once(args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_retention_run_once

    return cmd_pitr_retention_run_once(confirm=args.confirm)


def _h_pitr_snapshot_archive(args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_snapshot_archive

    return cmd_pitr_snapshot_archive(args.table)


def _h_pitr_snapshot_verify(args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_snapshot_verify

    return cmd_pitr_snapshot_verify(args.table)


def _h_pitr_snapshot_retire(args: argparse.Namespace) -> int:
    from cli.commands.pitr import cmd_pitr_snapshot_retire

    return cmd_pitr_snapshot_retire(args.table)


def _add_operations_parser(
    pitr_sub: argparse._SubParsersAction[argparse.ArgumentParser],
    status: Callable[[argparse.Namespace], int],
    retire: Callable[[argparse.Namespace], int],
) -> None:
    operations = pitr_sub.add_parser(
        "operations",
        help="inspect backup/PITR operation custody and retire blocked operations",
    )
    operations_sub = operations.add_subparsers(dest="operations_cmd", required=True)
    operations_status = operations_sub.add_parser(
        "status", help="show blocked operation kinds and quarantined operations"
    )
    operations_status.set_defaults(func=status)
    operations_retire = operations_sub.add_parser(
        "retire",
        help="re-prove group closure of blocked operations, then quarantine them",
    )
    operations_retire.add_argument(
        "--confirm",
        action="store_true",
        help="quarantine every proven operation; without it the command only previews",
    )
    operations_retire.set_defaults(func=retire)
    discard = operations_sub.add_parser(
        "discard-candidate",
        help="remove one unfinished base capture that blocks activation or keeps failing",
    )
    discard.add_argument("chain", metavar="CHAIN", help="the unfinished capture's chain id")
    discard.add_argument(
        "--confirm",
        action="store_true",
        help="remove the capture; without it the command only checks it",
    )
    discard.set_defaults(func=_h_pitr_operations_discard_candidate)


def _add_pitr_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import (
        _h_pitr_drill,
        _h_pitr_multipart_abort,
        _h_pitr_multipart_list,
        _h_pitr_operations_retire,
        _h_pitr_operations_status,
        _h_pitr_retention_arm,
        _h_pitr_retention_disable,
        _h_pitr_retention_inspect,
        _h_pitr_retention_run_once,
        _h_pitr_retention_status,
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
    drill_candidate = drill.add_mutually_exclusive_group(required=True)
    drill_candidate.add_argument(
        "--chain",
        metavar="CHAIN",
        help="chain id resolved under $AVA_HOME/physical-backup/base-manifests",
    )
    drill_candidate.add_argument(
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
        type=_target_wall_arg,
        help="target wall clock with an explicit UTC offset, e.g. '2026-09-13 13:13:03+08'",
    )
    drill.add_argument(
        "--scratch",
        metavar="DIR",
        required=True,
        help="fresh scratch directory (relative to the current directory); kept as evidence",
    )
    # task #4092 cli-default inventory: conservative replay bound — the drill
    # never forces, so a longer default only costs the operator's own wait; the
    # per-invocation flag tunes it.
    drill.add_argument(
        "--promotion-timeout",
        metavar="SECONDS",
        type=int,
        default=1800,
        help="bound for postmaster start and replay to the target",
    )
    drill.set_defaults(func=_h_pitr_drill)
    _add_operations_parser(pitr_sub, _h_pitr_operations_status, _h_pitr_operations_retire)
    retention = pitr_sub.add_parser(
        "retention", help="inspect the retention dry-run plan and operate its deletion gate"
    )
    retention_sub = retention.add_subparsers(dest="retention_cmd", required=True)
    inspect = retention_sub.add_parser("inspect", help="show the latest local dry-run plan")
    inspect.set_defaults(func=_h_pitr_retention_inspect)
    arm = retention_sub.add_parser(
        "arm",
        help="approve one plan digest and write the arm carriers (the only gate opener)",
    )
    arm.add_argument(
        "--digest",
        metavar="SHA256",
        required=True,
        help="plan digest to approve; read it from `retention status`",
    )
    arm.add_argument(
        "--confirm",
        action="store_true",
        help="write the carriers; without it the command only previews",
    )
    arm.set_defaults(func=_h_pitr_retention_arm)
    disable = retention_sub.add_parser(
        "disable", help="clear the arm carriers; back to the default-off dry-run"
    )
    disable.add_argument(
        "--confirm",
        action="store_true",
        help="clear the carriers; without it the command only previews",
    )
    disable.set_defaults(func=_h_pitr_retention_disable)
    gate_status = retention_sub.add_parser(
        "status", help="show carriers, the latest plan, and the deletion state machine"
    )
    gate_status.set_defaults(func=_h_pitr_retention_status)
    run_once = retention_sub.add_parser(
        "run-once",
        help="operator-present first deletion pass through the bounded executor",
    )
    run_once.add_argument(
        "--confirm",
        action="store_true",
        help="execute the pass; without it the command only validates",
    )
    run_once.set_defaults(func=_h_pitr_retention_run_once)
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
    multipart = pitr_sub.add_parser(
        "multipart", help="inspect and abort incomplete multipart uploads (orphan shards)"
    )
    multipart_sub = multipart.add_subparsers(dest="multipart_cmd", required=True)
    multipart_list = multipart_sub.add_parser(
        "list", help="list incomplete multipart uploads (read-only)"
    )
    multipart_list.add_argument(
        "--prefix",
        metavar="PREFIX",
        default="",
        help="only list uploads whose key starts with this prefix",
    )
    multipart_list.add_argument(
        "--credentials-file",
        metavar="PATH",
        help=(
            "OSS credential file to use; defaults to AVA_PITR_OSS_CREDENTIALS_FILE "
            "(the uploader identity)"
        ),
    )
    multipart_list.set_defaults(func=_h_pitr_multipart_list)
    multipart_abort = multipart_sub.add_parser(
        "abort", help="abort one incomplete multipart upload (preview without --confirm)"
    )
    multipart_abort.add_argument(
        "--key", metavar="KEY", required=True, help="the upload's object key"
    )
    multipart_abort.add_argument(
        "--upload-id",
        metavar="UPLOAD_ID",
        required=True,
        help="the upload id from `multipart list`",
    )
    multipart_abort.add_argument(
        "--credentials-file",
        metavar="PATH",
        help=(
            "OSS credential file to use; defaults to AVA_PITR_OSS_CREDENTIALS_FILE "
            "(the uploader identity)"
        ),
    )
    multipart_abort.add_argument(
        "--confirm",
        action="store_true",
        help="perform the abort; without it the command only previews",
    )
    multipart_abort.set_defaults(func=_h_pitr_multipart_abort)
