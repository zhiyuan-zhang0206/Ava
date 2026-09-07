"""`ava cluster` — whole-cluster verbs: argparse builder + its `_h_*` handlers.

Every verb here operates on the cluster as a whole (roster, rollout, rollback,
health/watchdog probes, registry lifecycle) rather than a single host — the
host-level set lives in ``cli.parsers.host``. Handlers lazy-import their
`cmd_*` implementation from ``cli.commands`` so parser building never loads
Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse


def _h_cluster_update(args: argparse.Namespace) -> int:
    from cli.commands import cmd_update

    return cmd_update(
        restart_only=args.restart_only,
        local=args.local,
        force=args.force,
        dry_run=args.dry_run,
        origin=args.origin,
        rollout_log=args.rollout_log,
        mode=args.mode,
    )


def _h_cluster_status(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_cluster_status

    return cmd_cluster_status()


def _h_cluster_mark_staging(args: argparse.Namespace) -> int:
    from cli.commands import cmd_cluster_mark_staging

    return cmd_cluster_mark_staging(name=args.name, is_staging=args.is_staging)


def _h_cluster_restart(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_cluster_restart

    return cmd_cluster_restart()


def _h_cluster_pause(args: argparse.Namespace) -> int:
    from cli.commands import cmd_cluster_pause

    return cmd_cluster_pause(name=args.name, reason=args.reason)


def _h_cluster_resume(args: argparse.Namespace) -> int:
    from cli.commands import cmd_cluster_resume

    return cmd_cluster_resume(name=args.name)


def _h_cluster_recover(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_cluster_recover

    return cmd_cluster_recover()


def _h_cluster_cancel(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_cluster_cancel

    return cmd_cluster_cancel()


def _h_cluster_pitr_activate(args: argparse.Namespace) -> int:
    from cli.commands import cmd_pitr_activate

    return cmd_pitr_activate(origin=args.origin)


def _h_cluster_pitr_status(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_pitr_status

    return cmd_pitr_status()


def _h_cluster_pitr_rollback(args: argparse.Namespace) -> int:
    from cli.commands import cmd_pitr_rollback

    return cmd_pitr_rollback(continuation=args.continuation)


def _h_cluster_ls(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_cluster_ls

    return cmd_cluster_ls()


def _h_cluster_down(args: argparse.Namespace) -> int:
    from cli.commands import cmd_cluster_down

    return cmd_cluster_down(path=args.path)


def _h_cluster_destroy(args: argparse.Namespace) -> int:
    from cli.commands import cmd_cluster_destroy

    return cmd_cluster_destroy(path=args.path, drop_db=args.drop_db)


def _h_cluster_health_probe(args: argparse.Namespace) -> int:
    from cli.commands import cmd_health_probe

    return cmd_health_probe(
        agent_min=args.agent_min,
        crash_loop_max_restarts=args.crash_loop_max_restarts,
        crash_loop_window_minutes=args.crash_loop_window_minutes,
        check_crash_loops=args.crash_loop_check,
        check_schema=args.schema_check,
        auto_rollback=args.auto_rollback,
        threshold=args.threshold,
    )


def _h_cluster_ensure_db_role(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_ensure_db_role

    return cmd_ensure_db_role()


def _h_cluster_rollback(args: argparse.Namespace) -> int:
    from cli.commands import cmd_rollback

    return cmd_rollback(
        to=args.to,
        set_known_good=args.set_known_good,
        keep_pin=args.keep_pin,
        require_confirmation=not args.yes,
    )


def _h_cluster_health_probe_register(args: argparse.Namespace) -> int:
    from cli.commands import cmd_cron_register

    return cmd_cron_register(
        interval_s=args.interval,
        threshold=args.threshold,
    )


def _h_cluster_health_probe_unregister(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_cron_unregister

    return cmd_cron_unregister()


def _h_cluster_watchdog_probe(args: argparse.Namespace) -> int:
    from cli.commands import cmd_watchdog_probe

    return cmd_watchdog_probe(args.role)


def _h_cluster_watchdog_probe_register(args: argparse.Namespace) -> int:
    from cli.commands import cmd_watchdog_probe_register

    return cmd_watchdog_probe_register(args.role)


def _h_cluster_watchdog_probe_unregister(args: argparse.Namespace) -> int:
    from cli.commands import cmd_watchdog_probe_unregister

    return cmd_watchdog_probe_unregister(args.role)


def _add_cluster_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # noqa: PLR0915
    from cli.main import (
        _h_cluster_cancel,
        _h_cluster_destroy,
        _h_cluster_down,
        _h_cluster_ensure_db_role,
        _h_cluster_health_probe,
        _h_cluster_health_probe_register,
        _h_cluster_health_probe_unregister,
        _h_cluster_ls,
        _h_cluster_pause,
        _h_cluster_pitr_activate,
        _h_cluster_pitr_rollback,
        _h_cluster_pitr_status,
        _h_cluster_recover,
        _h_cluster_restart,
        _h_cluster_resume,
        _h_cluster_rollback,
        _h_cluster_status,
        _h_cluster_update,
        _h_cluster_watchdog_probe,
        _h_cluster_watchdog_probe_register,
        _h_cluster_watchdog_probe_unregister,
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
    pitr_rollback_p.add_argument("--continuation", help=argparse.SUPPRESS)
    pitr_rollback_p.set_defaults(func=_h_cluster_pitr_rollback)
    for flag, help_text in (
        (
            "mark-staging",
            "[cluster] mark a machine as staging — registered + roster-visible, "
            "excluded from rollout targets (fan-out skips it). Operator-set; never touched by ava start",
        ),
        (
            "unmark-staging",
            "[cluster] clear the staging flag — the machine becomes a normal rollout target again",
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
            "hide it from roster/probe/rollout/spawn (no offline alerts). Registration "
            "kept for `ava cluster resume`",
            True,
        ),
        (
            "resume",
            "[cluster] restore a paused machine as a normal cluster member (clears the "
            "pause latch; probing/roster/rollout/spawn resume immediately). Prints the "
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

    cluster_restart_p = cluster_sub.add_parser(
        "restart",
        help="[cluster] bounce the whole cluster (this host + fan out to agent-runners, no git pull) "
        "via POST /api/cluster/restart; `ava restart` is the local single-host form",
    )
    cluster_restart_p.set_defaults(func=_h_cluster_restart)
    cluster_update_p = cluster_sub.add_parser(
        "update",
        help="[cluster] upgrade the whole cluster to the latest code (thin client: "
        "POSTs /api/cluster/rollout to the gateway from ANY host — user ruling "
        "2026-08-21, issue #216). The gateway's detached rollout session runs the "
        "three-phase orchestration (pause agent-runners -> local "
        "pull/sync/migrate/restart -> fan out agent-runner self-updates); a pure "
        "agent-runner is updated by that fan-out, not by this verb.",
    )
    update_kind = cluster_update_p.add_mutually_exclusive_group()
    update_kind.add_argument(
        "--restart-only",
        action="store_true",
        help="bounce every service on the current code (no git pull / uv sync / migration) "
        "— used to apply config changes cluster-wide. POSTs /api/cluster/restart "
        "to the gateway from any host.",
    )
    cluster_update_p.add_argument(
        "--local",
        action="store_true",
        default=False,
        help="run the in-process orchestration in this foreground process instead "
        "of POSTing the gateway. This is what the gateway's detached rollout "
        "session runs (it must not re-POST and recurse); also for debugging. An "
        "explicit flag — the default `ava cluster update` never branches by role.",
    )
    cluster_update_p.add_argument(
        "--origin",
        default=None,
        help="who triggered this update (e.g. 'agent:9'); recorded in the rollout log "
        "and the cluster pin's updated_by. Defaults to cli:<machine>; the detached "
        "rollout session threads it automatically.",
    )
    cluster_update_p.add_argument(
        "--rollout-log",
        default=None,
        help=argparse.SUPPRESS,
    )
    cluster_update_p.add_argument(
        "--force",
        action="store_true",
        help="start even though a deploy is in flight somewhere in the cluster. This "
        "SKIPS the deploy-window check without clearing anything, so it does NOT help "
        "against a crashed rollout: the orchestration takes cluster_update_lock after "
        "this check and will still abort for the lock's full TTL. For that, use "
        "`ava cluster recover`, which clears the stranded hold. Reach for --force only "
        "when the reported deploy is real but you intend to start anyway.",
    )
    update_kind.add_argument(
        "--dry-run",
        action="store_true",
        help="run prepare checks and report the maintenance-window estimate without snapshotting, "
        "pausing, stopping, or changing the cluster pin",
    )
    cluster_update_p.add_argument(
        "--mode",
        choices=("smooth", "force"),
        default="smooth",
        help="both modes require every native agent to finish restart/checkpoint drain "
        "before migration; 'force' explicitly permits forced resource shutdown afterward",
    )
    cluster_update_p.set_defaults(func=_h_cluster_update)

    cluster_cancel_p = cluster_sub.add_parser(
        "cancel",
        help="[cluster] cancel a live rollout/restart orchestration by interrupting its "
        "own process — its finally resumes paused hosts, releases (or settles) the "
        "deploy lease and clears the maintenance marker. Runs in-process on the "
        "orchestrating host; refuses when nothing provably live is there to cancel",
    )
    cluster_cancel_p.set_defaults(func=_h_cluster_cancel)

    cluster_recover_p = cluster_sub.add_parser(
        "recover",
        help="[cluster] clear a stranded update lock + paused posture left by a crashed "
        "rollout, so a new `ava cluster update` can start without waiting out the lock TTL. "
        "Refuses while the holder process is alive; runs in-process, so it works "
        "with the gateway down. This is the one to reach for after a crash — "
        "`ava cluster update --force` only skips the pre-flight check and still hits the "
        "stranded lock underneath it",
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
    cluster_health_probe_p.add_argument(
        "--auto-rollback",
        action="store_true",
        help="count consecutive failures and trigger rollback once --threshold is reached",
    )
    cluster_health_probe_p.add_argument(
        "--threshold",
        type=int,
        default=3,
        help="consecutive failures before triggering rollback (default: 3)",
    )
    cluster_health_probe_p.set_defaults(func=_h_cluster_health_probe)

    # --- `ava cluster ensure-db-role` ---
    # The one-shot legacy-cluster counterpart of install-time provisioning: the
    # SAME idempotent SQL a birth runs (ava_runner Postgres role + grants), plus
    # the AVA_RUNNER_DB_PASSWORD credential and a live pooler userlist refresh.
    # Named for the Postgres account it provisions (issue #217) — "runner role"
    # elsewhere means the machine capability; this verb has nothing to do with
    # machine capabilities. The old name stays as an alias for anything that
    # scripts it.
    cluster_ensure_db_role_p = cluster_sub.add_parser(
        "ensure-db-role",
        aliases=["ensure-runner-role"],
        help="[cluster] provision the least-privilege ava_runner Postgres role "
        "on THIS cluster (idempotent; runs the same SQL as install birth). For "
        "clusters born before the runner-role cutover — a fresh install does "
        "this automatically. Also writes AVA_RUNNER_DB_PASSWORD to the gateway "
        ".env and refreshes the pooler userlist when the pooler is running. "
        "Postgres must be up (`ava start` first).",
    )
    cluster_ensure_db_role_p.set_defaults(func=_h_cluster_ensure_db_role)

    # --- `ava cluster rollback` ---
    cluster_rollback_p = cluster_sub.add_parser(
        "rollback",
        help="[cluster] roll the cluster back to a known-good commit (stops agents, rolls back schema, restarts)",
    )
    cluster_rollback_p.add_argument(
        "--to",
        default=None,
        help="target commit (tag, SHA, or branch); default: last_known_good_sha from cluster_pin",
    )
    cluster_rollback_p.add_argument(
        "--set-known-good",
        action="store_true",
        help="after rollback, advance last_known_good_sha to the rolled-back-to commit",
    )
    cluster_rollback_p.add_argument(
        "--keep-pin",
        action="store_true",
        help="roll back only this gateway; leave the cluster pin and remote agent-runners unchanged",
    )
    cluster_rollback_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (for cron-triggered rollback)",
    )
    cluster_rollback_p.set_defaults(func=_h_cluster_rollback)

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
    cluster_health_probe_register_p.add_argument(
        "--threshold",
        type=int,
        default=3,
        help="consecutive failures before triggering rollback (default: 3)",
    )
    cluster_health_probe_register_p.set_defaults(func=_h_cluster_health_probe_register)

    # --- `ava cluster health-probe-unregister` ---
    cluster_health_probe_unregister_p = cluster_sub.add_parser(
        "health-probe-unregister",
        help="remove the OS-scheduled health probe",
    )
    cluster_health_probe_unregister_p.set_defaults(func=_h_cluster_health_probe_unregister)

    # --- `ava cluster watchdog-probe` ---
    # The command the OS scheduler runs every minute; also useful by hand to
    # check "would the probe revive this watchdog right now?".
    cluster_watchdog_probe_p = cluster_sub.add_parser(
        "watchdog-probe",
        help="respawn this capability's watchdog if its session is dead",
    )
    cluster_watchdog_probe_p.add_argument(
        "--role",
        required=True,
        choices=["gateway", "agent-runner"],
        help="which capability's watchdog to probe",
    )
    cluster_watchdog_probe_p.set_defaults(func=_h_cluster_watchdog_probe)

    # --- `ava cluster watchdog-probe-register` / `-unregister` ---
    # Manual counterparts to the converge step, for debugging a host whose job
    # went missing without re-running a full `ava start`.
    cluster_wp_register_p = cluster_sub.add_parser(
        "watchdog-probe-register",
        help="register the OS-scheduled watchdog probe for one capability",
    )
    cluster_wp_register_p.add_argument("--role", required=True, choices=["gateway", "agent-runner"])
    cluster_wp_register_p.set_defaults(func=_h_cluster_watchdog_probe_register)

    cluster_wp_unregister_p = cluster_sub.add_parser(
        "watchdog-probe-unregister",
        help="remove the OS-scheduled watchdog probe for one capability",
    )
    cluster_wp_unregister_p.add_argument(
        "--role", required=True, choices=["gateway", "agent-runner"]
    )
    cluster_wp_unregister_p.set_defaults(func=_h_cluster_watchdog_probe_unregister)
