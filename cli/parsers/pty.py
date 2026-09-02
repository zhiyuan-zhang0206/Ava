"""`ava pty` — host-wide PTY allocation admission commands."""

from __future__ import annotations

import argparse


def _h_pty_freeze(args: argparse.Namespace) -> int:
    from cli.commands import cmd_pty_freeze

    return cmd_pty_freeze(holder=args.holder, reason=args.reason)


def _h_pty_status(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_pty_status

    return cmd_pty_status()


def _h_pty_resume(args: argparse.Namespace) -> int:
    from cli.commands import cmd_pty_resume

    return cmd_pty_resume(generation=args.generation)


def _add_pty_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_pty_freeze, _h_pty_resume, _h_pty_status

    pty_p = sub.add_parser(
        "pty",
        help="[host] freeze, inspect, or resume host-wide allocation of new PTY sessions",
    )
    pty_sub = pty_p.add_subparsers(dest="pty_cmd", required=True)

    freeze_p = pty_sub.add_parser(
        "freeze",
        help="block every new PTY allocation without interrupting existing sessions",
    )
    freeze_p.add_argument("--holder", required=True, help="operator or workflow owning the freeze")
    freeze_p.add_argument("--reason", required=True, help="why new PTY allocation is frozen")
    freeze_p.set_defaults(func=_h_pty_freeze)

    status_p = pty_sub.add_parser("status", help="show the host freeze generation and metadata")
    status_p.set_defaults(func=_h_pty_status)

    resume_p = pty_sub.add_parser(
        "resume", help="resume allocation only when the exact owning generation is supplied"
    )
    resume_p.add_argument("generation", help="generation token printed by `ava pty freeze`")
    resume_p.set_defaults(func=_h_pty_resume)
