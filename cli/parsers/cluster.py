"""`ava cluster` — whole-cluster verbs: argparse builder + its `_h_*` handlers.

Every verb here operates on the cluster as a whole (roster, prepared release
submission, recovery, health probes, registry lifecycle) rather than a single host — the
host-level set lives in ``cli.parsers.host``. Handlers lazy-import their
`cmd_*` implementation from ``cli.commands`` so parser building never loads
Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse
from pathlib import Path

from shared.deploy_timing import UNIT_BUNDLE_MAX_TTL_S, UNIT_BUNDLE_TTL_S


def _h_cluster_update(args: argparse.Namespace) -> int:
    from cli.release_handoff.handoff import run

    return run(Path(args.prepared))


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


def _h_cluster_recover(_args: argparse.Namespace) -> int:
    from cli.commands.cluster.recover import cmd_cluster_recover

    return cmd_cluster_recover()


def _h_cluster_pitr_activate(args: argparse.Namespace) -> int:
    from cli.commands.data_plane.pitr_activation import cmd_pitr_activate

    return cmd_pitr_activate(origin=args.origin)


def _h_cluster_pitr_status(_args: argparse.Namespace) -> int:
    from cli.commands.data_plane.pitr_activation import cmd_pitr_status

    return cmd_pitr_status()


def _h_cluster_pitr_rollback(_args: argparse.Namespace) -> int:
    from cli.commands.data_plane.pitr_activation import cmd_pitr_rollback

    return cmd_pitr_rollback()


def _h_cluster_ls(_args: argparse.Namespace) -> int:
    from cli.commands.cluster.registry import cmd_cluster_ls

    return cmd_cluster_ls()


def _h_cluster_down(args: argparse.Namespace) -> int:
    from cli.commands.cluster.registry import cmd_cluster_down

    return cmd_cluster_down(path=args.path)


def _h_cluster_destroy(args: argparse.Namespace) -> int:
    from cli.commands.cluster.registry import cmd_cluster_destroy

    return cmd_cluster_destroy(path=args.path, drop_db=args.drop_db)


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


def _h_cluster_release_prepare(args: argparse.Namespace) -> int:
    from cli.release_operator.prepare import cmd_release_prepare

    return cmd_release_prepare(
        commit=args.commit,
        inputs=Path(args.inputs),
        repo=Path(args.repo) if args.repo is not None else None,
    )


def _h_cluster_release_request(args: argparse.Namespace) -> int:
    from cli.release_operator.request import cmd_release_request

    return cmd_release_request(
        commit=args.commit,
        out=Path(args.out),
        exclude=tuple(args.exclude),
        reason=args.reason,
        receipt=Path(args.receipt) if args.receipt is not None else None,
        watch_s=args.watch_s,
        alert_agent=args.alert_agent,
        alert_webhook_file=args.alert_webhook_file,
        acknowledged_rejection=args.acknowledged_rejection,
    )


def _h_cluster_release_exclude(args: argparse.Namespace) -> int:
    from cli.release_operator.exclude import cmd_release_exclude

    return cmd_release_exclude(operation=args.operation, unit=args.unit, reason=args.reason)


def _h_cluster_release_adopt(args: argparse.Namespace) -> int:
    from cli.release_operator.adopt import cmd_release_adopt

    return cmd_release_adopt(receipt=Path(args.receipt))


def _h_cluster_release_status(args: argparse.Namespace) -> int:
    from cli.release_operator.status import cmd_release_status

    return cmd_release_status(operation=args.operation, as_json=args.json)


def _h_cluster_db_authority_issue_unit(args: argparse.Namespace) -> int:
    from cli.commands.cluster.control import cmd_db_authority_issue_unit

    return cmd_db_authority_issue_unit(
        machine=args.machine, home=args.home, out=args.out, ttl_hours=args.ttl_hours
    )


def _h_cluster_db_authority_rotate_enrollment(args: argparse.Namespace) -> int:
    from cli.commands.cluster.control import cmd_db_authority_rotate_enrollment

    return cmd_db_authority_rotate_enrollment(machine=args.machine, home=args.home)


def _h_cluster_db_authority_revoke_enrollment(args: argparse.Namespace) -> int:
    from cli.commands.cluster.control import cmd_db_authority_revoke_enrollment

    return cmd_db_authority_revoke_enrollment(machine=args.machine, home=args.home)


def _add_enrollment_parsers(
    db_authority_sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    for verb, handler, help_text in (
        (
            "rotate-enrollment",
            _h_cluster_db_authority_rotate_enrollment,
            "on the gateway: replace one unit's enrollment secret (the release coordinator "
            "channel key); its next issue-unit bundle delivers the new one",
        ),
        (
            "revoke-enrollment",
            _h_cluster_db_authority_revoke_enrollment,
            "on the gateway: delete one unit's enrollment record; the unit can no longer "
            "authenticate to a release coordinator (its database login and API token stay "
            "valid until the write generation rotates)",
        ),
    ):
        verb_p = db_authority_sub.add_parser(verb, help=help_text)
        verb_p.add_argument("--machine", required=True, help="the unit's machine name")
        verb_p.add_argument(
            "--home", required=True, help="the unit's absolute $AVA_HOME path on its machine"
        )
        verb_p.set_defaults(func=handler)


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
        help="on the gateway: seal the active generation's runner login and the unit's "
        "enrollment secret into a 0600 bundle for one agent-runner unit (join, "
        "emergencies); prints its transport key once",
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
    _add_enrollment_parsers(db_authority_sub)


def _add_release_parser(cluster_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    release_p = cluster_sub.add_parser(
        "release",
        help="[cluster] release-operator verbs: prepare an image, build the fleet release "
        "request, adopt a first image, exclude a unit, read the release status",
    )
    release_sub = release_p.add_subparsers(dest="release_cmd", required=True)

    prepare_p = release_sub.add_parser(
        "prepare",
        help="[cluster release] build one inactive image on this host from an already "
        "acquired LocalInputs document; no outage",
    )
    prepare_p.add_argument("--commit", required=True, help="exact committed source SHA")
    prepare_p.add_argument(
        "--inputs",
        required=True,
        help="explicit LocalInputs JSON (see cli.release_prepare.acquire, or CI); this "
        "verb does not auto-discover build inputs",
    )
    prepare_p.add_argument(
        "--repo",
        default=None,
        help="source repository root (default: this checkout's own repo root)",
    )
    prepare_p.set_defaults(func=_h_cluster_release_prepare)

    request_p = release_sub.add_parser(
        "request",
        help="[cluster release] on the gateway home: build the fleet release request from "
        "this home's current selection, its prepared receipt and every registered unit; "
        "write it for `ava cluster update --prepared`",
    )
    request_p.add_argument(
        "--commit", required=True, help="candidate commit with an existing prepared receipt"
    )
    request_p.add_argument(
        "--receipt",
        default=None,
        help="explicit PreparationReceipt JSON (default: this home's receipt for --commit)",
    )
    request_p.add_argument(
        "--out", required=True, help="path to write the request JSON (0600; refuses if it exists)"
    )
    request_p.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="MACHINE:HOME",
        help="leave this registered unit out (it stays stale until it converges); repeatable",
    )
    request_p.add_argument("--reason", default=None, help="the recorded reason for every --exclude")
    request_p.add_argument(
        "--watch-s",
        type=int,
        default=None,
        help="the post-resume watch window in seconds (default: the fleet policy's)",
    )
    request_p.add_argument(
        "--alert-agent",
        type=int,
        default=None,
        metavar="AGENT_ID",
        help="also notify this observing agent of every fleet alert (it only observes)",
    )
    request_p.add_argument(
        "--alert-webhook-file",
        default=None,
        metavar="NAME",
        help="also POST every fleet alert to the webhook URL held in the owner-only "
        "$AVA_HOME/secrets/NAME (0600); it reaches a person while the cluster is down",
    )
    request_p.add_argument(
        "--acknowledged-rejection",
        default=None,
        metavar="OPERATION_ID",
        help="request a candidate an earlier operation rejected: name that exact "
        "(latest) rejecting operation",
    )
    request_p.set_defaults(func=_h_cluster_release_request)

    exclude_p = release_sub.add_parser(
        "exclude",
        help="[cluster release] on the gateway home: leave one unit out of a held fleet "
        "operation, or out of one that marked it failed or unknown (recorded)",
    )
    exclude_p.add_argument("--operation", required=True, help="the fleet operation id")
    exclude_p.add_argument("--unit", required=True, metavar="MACHINE:HOME", help="the unit")
    exclude_p.add_argument("--reason", required=True, help="the recorded reason")
    exclude_p.set_defaults(func=_h_cluster_release_exclude)

    adopt_p = release_sub.add_parser(
        "adopt",
        help="[cluster release] first image selection for a source-run home "
        "(activate_release(expected_current=None) + the steady boot action); "
        "requires a stopped root, Linux only",
    )
    adopt_p.add_argument(
        "--receipt", required=True, help="PreparationReceipt JSON from `release prepare`"
    )
    adopt_p.set_defaults(func=_h_cluster_release_adopt)

    status_p = release_sub.add_parser(
        "status",
        help="[cluster release] read-only view of this home's current release selection, "
        "the published fleet release state and the fleet or unit operation journal",
    )
    status_p.add_argument(
        "--operation",
        default=None,
        help="explicit operation id (default: this home's active operation, if any)",
    )
    status_p.add_argument(
        "--json", action="store_true", default=False, help="machine-readable output"
    )
    status_p.set_defaults(func=_h_cluster_release_status)


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
            "journal env + ALTER SYSTEM archive settings, restart through a finite PITR "
            "operation, prove WAL, then force and restore one exact base chain (default off)"
        ),
    )
    pitr_activate_p.add_argument(
        "--origin", default="cli", help="operator/agent identity recorded in the durable operation"
    )
    pitr_activate_p.set_defaults(func=_h_cluster_pitr_activate)
    pitr_rollback_p = pitr_sub.add_parser(
        "rollback",
        help=(
            "restore Ava-owned ALTER SYSTEM settings through the same finite PITR operation; "
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

    _add_db_authority_parser(cluster_sub)
    _add_release_parser(cluster_sub)

    cluster_update_p = cluster_sub.add_parser(
        "update",
        help="[cluster] submit or resume one captured immutable release operation: verify "
        "its executor image in this home's store and hand off to that image",
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
