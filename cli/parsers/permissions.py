from __future__ import annotations

import argparse


def _h_permissions(args: argparse.Namespace) -> int:
    from cli.commands.permissions import run

    return run(args)


def add_permissions_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    permissions_p = sub.add_parser(
        "permissions",
        help="macOS permissions: inventory, request, and the one-page checklist",
    )
    permissions_sub = permissions_p.add_subparsers(dest="permissions_cmd", required=True)

    status_p = permissions_sub.add_parser(
        "status",
        help="read the real grant matrix and print the checklist (exit 0 = complete)",
    )
    status_p.add_argument("--json", action="store_true", help="machine-readable payload")
    status_p.set_defaults(func=_h_permissions)

    onboard_p = permissions_sub.add_parser(
        "onboard",
        help=(
            "request every grant a dialog can still resolve (including denied ones), then re-read"
        ),
    )
    onboard_p.add_argument("--timeout", type=float, default=None, help="seconds per request")
    onboard_p.add_argument("--json", action="store_true", help="machine-readable payload")
    onboard_p.set_defaults(func=_h_permissions)

    defer_p = permissions_sub.add_parser(
        "defer",
        help='record "later": the host stays restricted and the next start asks again',
    )
    defer_p.set_defaults(func=_h_permissions)

    restart_p = permissions_sub.add_parser(
        "restart-helper",
        help=(
            "print how to recreate the helper so a freshly granted screen/accessibility"
            " sticks; it runs no process control of its own"
        ),
    )
    restart_p.set_defaults(func=_h_permissions)
