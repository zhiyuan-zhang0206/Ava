"""`ava packages` — the per-machine package refresh surface.

Builders plus their `_h_*` handlers. Handlers lazy-import their `cmd_*`
implementation from ``cli.commands`` so parser building never loads Settings
(see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse
import sys


def _h_packages_status(args: argparse.Namespace) -> int:
    from cli.commands.packages import cmd_packages_status

    return cmd_packages_status(json_output=args.json_output)


def _h_packages_refresh(args: argparse.Namespace) -> int:
    from cli.commands.packages import cmd_packages_refresh

    return cmd_packages_refresh(
        check_only=args.check,
        only=args.package,
        json_output=args.json_output,
        force=args.force,
        from_job=args.from_job,
    )


def _h_packages_rollback(args: argparse.Namespace) -> int:
    from cli.commands.packages import cmd_packages_rollback

    return cmd_packages_rollback(args.name, force=args.force)


def _duration(value: str) -> str:
    """Argparse type for `--check-every`: validate the duration before any command runs."""
    from cli.commands._packages_refresh import parse_duration

    try:
        parse_duration(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def _h_packages_policy(args: argparse.Namespace) -> int:
    from cli.commands.packages import cmd_packages_policy

    if args.update_mode is None and args.check_every is None:
        print(
            "ava: pass --update-mode and/or --check-every — a policy needs at least one field",
            file=sys.stderr,
        )
        return 2
    return cmd_packages_policy(
        args.name, update_mode=args.update_mode, check_every=args.check_every
    )


def _add_packages_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_packages_status

    packages_p = sub.add_parser(
        "packages",
        help="inspect per-package update policy, channels, and refresh state (this machine)",
    )
    packages_sub = packages_p.add_subparsers(dest="packages_cmd", required=True)

    status_p = packages_sub.add_parser(
        "status",
        help="host version + per-package channel / policy / applied rev / last result",
    )
    status_p.add_argument(
        "--json", action="store_true", dest="json_output", help="machine-readable output"
    )
    status_p.set_defaults(func=_h_packages_status)

    refresh_p = packages_sub.add_parser(
        "refresh",
        help="check the channels and apply due updates (skills; this machine)",
    )
    refresh_p.add_argument("--check", action="store_true", help="check only — never stage or apply")
    refresh_p.add_argument(
        "--package", metavar="NAME", default=None, help="limit the pass to one package"
    )
    refresh_p.add_argument(
        "--force",
        action="store_true",
        help="override the local-edit guard for this run (human-only)",
    )
    refresh_p.add_argument(
        "--from-job",
        action="store_true",
        dest="from_job",
        help="OS-job invocation: adds the job gates and respects each package's check cadence",
    )
    refresh_p.add_argument(
        "--json", action="store_true", dest="json_output", help="machine-readable output"
    )
    refresh_p.set_defaults(func=_h_packages_refresh)

    rollback_p = packages_sub.add_parser(
        "rollback",
        help="restore a package's previous tree (the .<name>.prev kept by the last apply)",
    )
    rollback_p.add_argument("name")
    rollback_p.add_argument("--force", action="store_true", help="override the local-edit guard")
    rollback_p.set_defaults(func=_h_packages_rollback)

    policy_p = packages_sub.add_parser(
        "policy",
        help="record an explicit update policy (mode / check interval) on one package",
    )
    policy_p.add_argument("name")
    policy_p.add_argument("--update-mode", choices=["auto", "notify", "off"], default=None)
    policy_p.add_argument(
        "--check-every", metavar="DUR", default=None, type=_duration, help="e.g. 30m / 24h / 7d"
    )
    policy_p.set_defaults(func=_h_packages_policy)
