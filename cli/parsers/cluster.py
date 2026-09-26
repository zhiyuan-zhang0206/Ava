"""`ava cluster` — whole-cluster verbs: argparse builder + its `_h_*` handlers.

Every verb here operates on the cluster as a whole (roster, prepared release
submission, recovery, health probes, registry lifecycle) rather than a single host — the
host-level set lives in ``cli.parsers.host``. Handlers lazy-import their
`cmd_*` implementation from ``cli.commands`` so parser building never loads
Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse
from pathlib import Path


def _h_cluster_update(args: argparse.Namespace) -> int:
    from cli.release_transition.submit import run

    return run(Path(args.prepared))


def _h_cluster_status(_args: argparse.Namespace) -> int:
    from cli.commands.cluster import cmd_cluster_status

    return cmd_cluster_status()


def _h_cluster_mark_staging(args: argparse.Namespace) -> int:
    from cli.commands.cluster import cmd_cluster_mark_staging

    return cmd_cluster_mark_staging(name=args.name, is_staging=args.is_staging)


def _h_cluster_pause(args: argparse.Namespace) -> int:
    from cli.commands.cluster import cmd_cluster_pause

    return cmd_cluster_pause(name=args.name, reason=args.reason)


def _h_cluster_resume(args: argparse.Namespace) -> int:
    from cli.commands.cluster import cmd_cluster_resume

    return cmd_cluster_resume(name=args.name)


def _h_cluster_recover(_args: argparse.Namespace) -> int:
    from cli.commands._cluster_recover import cmd_cluster_recover

    return cmd_cluster_recover()


def _h_cluster_pitr_activate(args: argparse.Namespace) -> int:
    from cli.commands._pitr_activation import cmd_pitr_activate

    return cmd_pitr_activate(origin=args.origin)


def _h_cluster_pitr_status(_args: argparse.Namespace) -> int:
    from cli.commands._pitr_activation import cmd_pitr_status

    return cmd_pitr_status()


def _h_cluster_pitr_rollback(_args: argparse.Namespace) -> int:
    from cli.commands._pitr_activation import cmd_pitr_rollback

    return cmd_pitr_rollback()


def _h_cluster_ls(_args: argparse.Namespace) -> int:
    from cli.commands.cluster_lifecycle import cmd_cluster_ls

    return cmd_cluster_ls()


def _h_cluster_down(args: argparse.Namespace) -> int:
    from cli.commands.cluster_lifecycle import cmd_cluster_down

    return cmd_cluster_down(path=args.path)


def _h_cluster_destroy(args: argparse.Namespace) -> int:
    from cli.commands.cluster_lifecycle import cmd_cluster_destroy

    return cmd_cluster_destroy(path=args.path, drop_db=args.drop_db)


def _h_cluster_health_probe(args: argparse.Namespace) -> int:
    from cli.commands._cluster_health import cmd_health_probe

    return cmd_health_probe(
        agent_min=args.agent_min,
        crash_loop_max_restarts=args.crash_loop_max_restarts,
        crash_loop_window_minutes=args.crash_loop_window_minutes,
        check_crash_loops=args.crash_loop_check,
        check_schema=args.schema_check,
    )


def _h_cluster_health_probe_register(args: argparse.Namespace) -> int:
    from cli.commands._cluster_cron import cmd_cron_register

    return cmd_cron_register(
        interval_s=args.interval,
    )


def _h_cluster_health_probe_unregister(_args: argparse.Namespace) -> int:
    from cli.commands._cluster_cron import cmd_cron_unregister

    return cmd_cron_unregister()


def _add_cluster_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import (
        _h_cluster_destroy,
        _h_cluster_down,
        _h_cluster_health_probe,
        _h_cluster_health_probe_register,
        _h_cluster_health_probe_unregister,
        _h_cluster_ls,
        _h_cluster_pause,
        _h_cluster_pitr_activate,
        _h_cluster_pitr_rollback,
        _h_cluster_pitr_status,
        _h_cluster_recover,
        _h_cluster_resume,
        _h_cluster_status,
        _h_cluster_update,
    )

    # `ava cluster status` — list machines table + per-agent-runner status_probe op
    cluster_p = sub.add_parser(
        "cluster",
        help="[cluster] cluster subcommands — every verb here operates on the whole cluster",
    )
    cluster_sub = cluster_p.add_subparsers(dest="cluster_cmd", required=True)
    cluster_status_p = cluster_sub.add_parser(
        "status",
        help="[cluster] full multi-machine roster (thin client: GET /api/cluster/roster; "
        "gateway assembles it + probes each agent-runner server-side)",
    )
    cluster_status_p.set_defaults(func=_h_cluster_status)
    pitr_p = cluster_sub.add_parser(
        "pitr",
        help="[cluster] explicit physical-backup activation lifecycle",
    )
    pitr_sub = pitr_p.add_subparsers(dest="pitr_cmd", required=True)
    pitr_status_p = pitr_sub.add_parser(
        "status", help="show the durable activation phase and original start time"
    )
    pitr_status_p.set_defaults(func=_h_cluster_pitr_status)
    pitr_activate_p = pitr_sub.add_parser(
        "activate",
        help=(
            "journal env + ALTER SYSTEM archive settings, restart the cluster, prove WAL, "
            "then force and restore one exact base chain (default off)"
        ),
    )
    pitr_activate_p.add_argument(
        "--origin", default="cli", help="operator/agent identity recorded in the durable operation"
    )
    pitr_activate_p.set_defaults(func=_h_cluster_pitr_activate)
    pitr_rollback_p = pitr_sub.add_parser(
        "rollback",
        help=(
            "restore Ava-owned env/ALTER SYSTEM settings through the same cluster restart; "
            "never delete backup objects"
        ),
    )
    pitr_rollback_p.set_defaults(func=_h_cluster_pitr_rollback)
    for flag, help_text in (
        (
            "mark-staging",
            "[cluster] mark a machine as staging — registered + roster-visible, "
            "excluded from agent-runner fan-outs and probes. Operator-set; never touched by ava start",
        ),
        (
            "unmark-staging",
            "[cluster] clear the staging flag — the machine becomes a normal fan-out target again",
        ),
    ):
        p_ = cluster_sub.add_parser(
            flag,
            help=help_text,
        )
        p_.add_argument("name", help="machine name (the machines-table row, e.g. the hostname)")
        p_.set_defaults(func=_h_cluster_mark_staging, is_staging=(flag == "mark-staging"))
    for verb, help_text, with_reason in (
        (
            "pause",
            "[cluster] temporarily pull a machine out of the cluster: drain its tasks "
            "(reassign in_progress to #405 with a note), terminate its agents, then "
            "hide it from roster/probe/fan-out/spawn (no offline alerts). Registration "
            "kept for `ava cluster resume`",
            True,
        ),
        (
            "resume",
            "[cluster] restore a paused machine as a normal cluster member (clears the "
            "pause latch; probing/roster/fan-out/spawn resume immediately). Prints the "
            "machine-side checklist (re-`ava start`, pg_hba if the reachable address changed)",
            False,
        ),
    ):
        p_ = cluster_sub.add_parser(verb, help=help_text)
        p_.add_argument("name", help="machine name (the machines-table row, e.g. the hostname)")
        if with_reason:
            p_.add_argument(
                "--reason",
                default=None,
                help="free-text why the machine is being pulled out (recorded on the "
                "machines row as pause_reason for the resume checklist)",
            )
        p_.set_defaults(func=_h_cluster_pause if verb == "pause" else _h_cluster_resume)

    cluster_update_p = cluster_sub.add_parser(
        "update",
        help="[cluster] submit or resume one captured immutable release operation",
    )
    cluster_update_p.add_argument(
        "--prepared",
        metavar="REQUEST",
        required=True,
        help="captured release request; repeated submission reconciles the same operation",
    )
    cluster_update_p.set_defaults(func=_h_cluster_update)

    cluster_recover_p = cluster_sub.add_parser(
        "recover",
        help="[cluster] recover an abandoned maintenance lease; refuses live ownership. "
        "Prepared release operations continue by resubmitting their captured request",
    )
    cluster_recover_p.set_defaults(func=_h_cluster_recover)

    cluster_ls_p = cluster_sub.add_parser("ls", help="[cluster] list all registered clusters")
    cluster_ls_p.set_defaults(func=_h_cluster_ls)

    cluster_down_p = cluster_sub.add_parser(
        "down",
        help="[cluster] stop the cluster at a home path (its services + its own pg/redis; "
        "keeps the registry entry + data dirs — the safe way to stop a dev "
        "worktree cluster from another checkout)",
    )
    cluster_down_p.add_argument(
        "--path", required=True, help="the cluster's home path (e.g. ~/.ava-mytask)"
    )
    cluster_down_p.set_defaults(func=_h_cluster_down)

    cluster_destroy_p = cluster_sub.add_parser(
        "destroy",
        help="[cluster] stop the cluster at a home path and remove its registry entry (frees "
        "its port block); refused for the default home (~/.ava, prod)",
    )
    cluster_destroy_p.add_argument(
        "--path", required=True, help="the cluster's home path (e.g. ~/.ava-mytask)"
    )
    cluster_destroy_p.add_argument(
        "--drop-db",
        action="store_true",
        default=False,
        help="also remove the cluster's own pg/redis data dirs (default: keep data)",
    )
    cluster_destroy_p.set_defaults(func=_h_cluster_destroy)

    # --- `ava cluster health-probe` ---
    cluster_health_probe_p = cluster_sub.add_parser(
        "health-probe",
        help="[cluster] assess cluster health (exit 0=healthy, 1=unhealthy); designed as a cron job payload",
    )
    # task #4092 cli-default inventory: monitoring contract — this verb is a
    # cron payload invoked bare; these defaults (agent-min from
    # AVA_HEALTH_PROBE_AGENT_MIN, itself 1) are what "healthy" means out of the box.
    cluster_health_probe_p.add_argument(
        "--agent-min",
        type=int,
        default=None,
        help="minimum running/idling agents for healthy verdict (default: AVA_HEALTH_PROBE_AGENT_MIN, itself 1)",
    )
    cluster_health_probe_p.add_argument(
        "--crash-loop-max-restarts",
        type=int,
        default=5,
        help="max restarts per agent in the window (default: 5)",
    )
    cluster_health_probe_p.add_argument(
        "--crash-loop-window-minutes",
        type=int,
        default=10,
        help="crash-loop detection window in minutes (default: 10)",
    )
    cluster_health_probe_p.add_argument(
        "--crash-loop-check",
        action="store_false",
        default=True,
        help="run the crash-loop detection check (default: enabled)",
    )
    cluster_health_probe_p.add_argument(
        "--schema-check",
        action="store_false",
        default=True,
        help="run the schema health check (default: enabled)",
    )
    cluster_health_probe_p.set_defaults(func=_h_cluster_health_probe)

    # --- `ava cluster health-probe-register` ---
    cluster_health_probe_register_p = cluster_sub.add_parser(
        "health-probe-register",
        help="register the OS-scheduled health probe (launchd on macOS, crontab on Linux)",
    )
    cluster_health_probe_register_p.add_argument(
        "--interval",
        type=int,
        default=300,
        help="seconds between health probe runs (default: 300 = 5 min)",
    )
    cluster_health_probe_register_p.set_defaults(func=_h_cluster_health_probe_register)

    # --- `ava cluster health-probe-unregister` ---
    cluster_health_probe_unregister_p = cluster_sub.add_parser(
        "health-probe-unregister",
        help="remove the OS-scheduled health probe",
    )
    cluster_health_probe_unregister_p.set_defaults(func=_h_cluster_health_probe_unregister)
