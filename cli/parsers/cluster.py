"""`ava cluster` — whole-cluster verbs: argparse builder + its `_h_*` handlers.

Every verb here operates on the cluster as a whole (roster, recovery, health
probes, home lifecycle) rather than a single host — the
host-level set lives in ``cli.parsers.host``. Handlers lazy-import their
`cmd_*` implementation from ``cli.commands`` so parser building never loads
Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse

from base.deploy.progress_timeout import UNIT_BUNDLE_MAX_TTL_S, UNIT_BUNDLE_TTL_S


def _h_cluster_status(_args: argparse.Namespace) -> int:
    from cli.commands.cluster.control import cmd_cluster_status

    return cmd_cluster_status()


def _h_cluster_mark_staging(args: argparse.Namespace) -> int:
    from cli.commands.cluster.control import cmd_cluster_mark_staging

    return cmd_cluster_mark_staging(name=args.name, is_staging=args.is_staging)


def _h_cluster_pause(args: argparse.Namespace) -> int:
    from cli.commands.cluster.control import cmd_cluster_pause

    return cmd_cluster_pause(name=args.name, reason=args.reason)


def _h_cluster_resume(args: argparse.Namespace) -> int:
    from cli.commands.cluster.control import cmd_cluster_resume

    return cmd_cluster_resume(name=args.name)


def _h_cluster_destroy(args: argparse.Namespace) -> int:
    from cli.commands.cluster.home import cmd_cluster_destroy

    return cmd_cluster_destroy(drop_db=args.drop_db)


def _h_cluster_health_probe(args: argparse.Namespace) -> int:
    from cli.commands.cluster.health import cmd_health_probe

    return cmd_health_probe(
        agent_min=args.agent_min,
        crash_loop_max_restarts=args.crash_loop_max_restarts,
        crash_loop_window_minutes=args.crash_loop_window_minutes,
        check_crash_loops=args.crash_loop_check,
        check_schema=args.schema_check,
    )


def _h_cluster_health_probe_register(args: argparse.Namespace) -> int:
    from cli.commands.cluster.cron import cmd_cron_register

    return cmd_cron_register(
        interval_s=args.interval,
    )


def _h_cluster_health_probe_unregister(_args: argparse.Namespace) -> int:
    from cli.commands.cluster.cron import cmd_cron_unregister

    return cmd_cron_unregister()


def _h_cluster_db_authority_issue_unit(args: argparse.Namespace) -> int:
    from cli.commands.cluster.control import cmd_db_authority_issue_unit

    return cmd_db_authority_issue_unit(
        machine=args.machine, home=args.home, out=args.out, ttl_hours=args.ttl_hours
    )


def _h_cluster_db_authority_install_unit(args: argparse.Namespace) -> int:
    from cli.commands.cluster.control import cmd_db_authority_install_unit

    return cmd_db_authority_install_unit(bundle=args.bundle)


def _add_db_authority_parser(
    cluster_sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    db_authority_p = cluster_sub.add_parser(
        "db-authority",
        help="[cluster] database write-generation authority for remote agent-runner units",
    )
    db_authority_sub = db_authority_p.add_subparsers(dest="db_authority_cmd", required=True)
    issue_unit_p = db_authority_sub.add_parser(
        "issue-unit",
        help="on the gateway: seal the active generation's runner login into a 0600 "
        "bundle for one agent-runner unit (join, emergencies); prints its transport key once",
    )
    issue_unit_p.add_argument("--machine", required=True, help="the unit's machine name")
    issue_unit_p.add_argument(
        "--home", required=True, help="the unit's absolute $AVA_HOME path on its machine"
    )
    issue_unit_p.add_argument(
        "--out", required=True, help="bundle path to create (refused when it exists)"
    )
    issue_unit_p.add_argument(
        "--ttl-hours",
        type=float,
        default=UNIT_BUNDLE_TTL_S / 3600,
        help=f"bundle lifetime in hours (default: {UNIT_BUNDLE_TTL_S / 3600:g}, "
        f"at most {UNIT_BUNDLE_MAX_TTL_S / 3600:g})",
    )
    issue_unit_p.set_defaults(func=_h_cluster_db_authority_issue_unit)
    install_unit_p = db_authority_sub.add_parser(
        "install-unit",
        help="on an initialized agent-runner unit: install a capability bundle `issue-unit` "
        "sealed for it (a fresh expiry, a rotated telemetry token); the transport key comes "
        "from AVA_DB_CAPABILITY_KEY and the bundle file is deleted once installed. Stop the "
        "unit first and `ava start` it after",
    )
    install_unit_p.add_argument("bundle", metavar="BUNDLE", help="path of the sealed bundle file")
    install_unit_p.set_defaults(func=_h_cluster_db_authority_install_unit)


def _add_cluster_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
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

    _add_db_authority_parser(cluster_sub)

    cluster_destroy_p = cluster_sub.add_parser(
        "destroy",
        help="[cluster] decommission this host's cluster: stop it, retire its OS jobs and mark "
        "the home detached. Asks you to type the home path, and needs an interactive terminal",
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
        "--no-crash-loop-check",
        action="store_false",
        dest="crash_loop_check",
        default=True,
        help="disable the crash-loop detection check (default: enabled)",
    )
    cluster_health_probe_p.add_argument(
        "--no-schema-check",
        action="store_false",
        dest="schema_check",
        default=True,
        help="disable the schema health check (default: enabled)",
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
