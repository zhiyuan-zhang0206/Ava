"""`ava` host-level lifecycle verbs — argparse builders + their `_h_*` handlers.

`start` / `pause` / `stop` / `restart` / `status` / `converge` / `firewall` / `trace` /
`lgtm` act
on THIS host (or the unit this checkout owns), as opposed to the cluster-wide
verbs in ``cli.parsers.cluster``. Handlers stay thin: each lazy-imports its
`cmd_*` implementation from ``cli.commands`` so building the parser never loads
Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse


def _h_start(args: argparse.Namespace) -> int:
    # The installed-home gate already ran in main() (cli.preflight, settings-free,
    # BEFORE this handler's cli.commands import can trip a generic Settings
    # validation error on an uninstalled home).
    from cli.commands import cmd_start

    return cmd_start(
        machine_name=args.machine_name,
        serve_gateway=args.serve_gateway,
        serve_agent_runner=args.serve_agent_runner,
        serve_observability_station=args.serve_observability_station,
        machine_description=args.machine_description,
        memory_remote=args.memory_remote,
        gateway_url=args.gateway_url,
        disabled_services=tuple(args.disable_service),
        persist_services=args.persist_services,
        readiness_gate=not args.no_readiness_gate,
        updater_telemetry=args.updater_telemetry,
    )


def _h_stop(args: argparse.Namespace) -> int:
    from cli.commands import cmd_stop

    return cmd_stop(
        keep_infra=args.keep_infra,
        require_confirmation=not args.yes,
        stop_browser=args.stop_browser,
        preserve_sessions=frozenset(args.keep_service),
        force=args.force,
        timeout=args.timeout,
    )


def _h_pause(args: argparse.Namespace) -> int:
    from cli.commands.stop import cmd_pause

    return cmd_pause(
        preserve_sessions=frozenset(args.keep_service), force=args.force, timeout=args.timeout
    )


def _h_restart(args: argparse.Namespace) -> int:
    from cli.commands import cmd_restart

    return cmd_restart(
        quiesce=args.quiesce,
        mode=args.mode,
        force_reap=args.force_reap,
    )


def _h_status(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_status

    return cmd_status()


def _h_converge(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_converge

    return cmd_converge()


def _h_firewall_status(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_firewall_status

    return cmd_firewall_status()


def _h_firewall_sync(_args: argparse.Namespace) -> int:
    from cli.commands import cmd_firewall_sync

    return cmd_firewall_sync()


def _h_trace_ship(args: argparse.Namespace) -> int:
    from cli.commands import cmd_trace_ship

    return cmd_trace_ship(since=args.since, until=args.until, dry_run=args.dry_run)


def _add_start_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_start

    # `ava start` — multi-machine setup args; pass once on first run, CLI persists
    # to file and subsequent calls do not need them. NO TTY prompt — agent-first
    # design, agent has no TTY, missing values fail loud.
    start_p = sub.add_parser(
        "start",
        help="[host] bring up this unit's full stack (idempotent). Cluster identity "
        "is checkout-anchored — never a flag. Machine identity is first-run only: "
        "pass --machine-name / --serve-gateway / --serve-agent-runner / "
        "--gateway-url on the FIRST start (or set the env vars / $AVA_HOME files); "
        "the values are persisted and later runs need none of them.",
    )
    start_p.add_argument(
        "--machine-name",
        default=None,
        help="usually first-run only: stable identifier for this host (e.g. host-a / host-b). "
        "Persisted to $AVA_HOME/machine_name; env AVA_MACHINE_NAME wins over the file",
    )
    start_p.add_argument(
        "--serve-gateway",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="usually first-run only: serve the gateway capability (central pg/redis + all daemons). Single box "
        "passes both --serve-gateway --serve-agent-runner; unset falls back to the "
        "$AVA_HOME/machine_serve_gateway file. env: AVA_MACHINE_SERVE_GATEWAY",
    )
    start_p.add_argument(
        "--serve-agent-runner",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="usually first-run only: serve the agent-runner capability (agent-host/ops/watchdog); unset falls back to the $AVA_HOME/machine_serve_agent_runner file. "
        "env: AVA_MACHINE_SERVE_AGENT_RUNNER",
    )
    start_p.add_argument(
        "--serve-observability-station",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="usually first-run only: serve the observability-station capability (own the native LGTM "
        "observability backends — the declarative form of the $AVA_HOME/lgtm-host marker); unset falls "
        "back to the $AVA_HOME/machine_serve_observability_station file. env: "
        "AVA_MACHINE_SERVE_OBSERVABILITY_STATION",
    )
    start_p.add_argument(
        "--machine-description",
        default=None,
        help='Free-text note of what this host is for (e.g. "voice IO + browser"); persisted to $AVA_HOME/machine_description and the machines table.',
    )
    start_p.add_argument(
        "--memory-remote",
        default=None,
        help="usually first-run only: central git remote URL for memory pool (e.g. git@github.com:you/AvaMemory.git). env: AVA_MEMORY_REMOTE",
    )
    start_p.add_argument(
        "--gateway-url",
        default=None,
        help="usually first-run only: public URL of the gateway. On the gateway this host's own URL; on an agent-runner, the gateway it reaches. env: AVA_GATEWAY_URL",
    )
    start_p.add_argument(
        "--disable-service",
        action="append",
        default=[],
        metavar="SERVICE",
        help="durably disable this service session (repeatable; e.g. --disable-service labeler "
        "--disable-service frontend). Pass the bare service name. The disable is recorded so the "
        "watchdog leaves it down; re-enable by running `ava start` again without the flag.",
    )
    start_p.add_argument(
        # Internal: an update / recovery / restart forwards its transient disabled set
        # (e.g. leave frontend running) without rewriting the operator's durable
        # --disable-service marker. Hidden from --help; operators never pass it.
        "--persist-services",
        action="store_false",
        default=True,
        help=argparse.SUPPRESS,
    )
    start_p.add_argument(
        # Internal: a detached updater asks the fresh `ava start` process to emit
        # migration/readiness timing without changing normal start behavior.
        "--updater-telemetry",
        action="store_true",
        default=False,
        help=argparse.SUPPRESS,
    )
    start_p.add_argument(
        "--no-readiness-gate",
        action="store_true",
        default=False,
        help="exit 0 even when a launched service never passes its liveness probe "
        "(default: exit 4 and name it, after the status snapshot). For callers that "
        "retry without a cap — the OS boot job passes this, because an unbounded "
        "retry on a permanently-unready service is a host that never finishes "
        "booting — or that answer readiness themselves, like the rollout's off-box "
        "gateway gate. The wait and the printed crosses are unaffected.",
    )
    start_p.set_defaults(func=_h_start)


def _add_stop_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_stop

    stop_p = sub.add_parser(
        "stop",
        help="[host] stop services, terminals and data plane; preserve data and agent identities",
    )
    stop_p.add_argument(
        "--keep-infra",
        action="store_true",
        help="do not stop THIS cluster's own Postgres/Redis instance (every "
        "cluster owns one, under its $AVA_HOME). Used by the `ava cluster update` "
        "orchestrator: the migrate step that follows still needs the database, so "
        "tearing the data plane down first would give it connect-refused. A plain "
        "`ava stop` means 'fully stop' and leaves this off.",
    )
    stop_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the stdin y/N confirmation (non-interactive / scripted use).",
    )
    stop_p.add_argument(
        "--stop-browser",
        action="store_true",
        default=True,
        help=argparse.SUPPRESS,
    )
    _add_stop_options(stop_p)
    stop_p.set_defaults(func=_h_stop)


def _add_stop_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--keep-service",
        action="append",
        default=[],
        metavar="SERVICE",
        help="retain a named service (repeatable); DB-dependent services require --keep-infra",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="total normal drain/stop deadline; failure never silently forces",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="explicitly permit force-killing work that cannot exit normally",
    )


def _add_pause_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_pause

    parser = sub.add_parser(
        "pause",
        help="[host] pause for maintenance; retain data plane, browser and persistent terminals",
    )
    _add_stop_options(parser)
    parser.set_defaults(func=_h_pause)


def _add_restart_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_restart

    restart_p = sub.add_parser(
        "restart",
        help="[host] normal pause then start, retaining persistent terminals",
    )
    restart_p.add_argument(
        "--quiesce",
        action="store_true",
        help="compatibility flag; restart always uses the native drain boundary",
    )
    restart_p.add_argument(
        "--mode",
        choices=("smooth", "force"),
        default="smooth",
        help="'smooth' preserves completed work; 'force' explicitly permits forced resource shutdown",
    )
    restart_p.add_argument(
        "--force-reap",
        action="store_true",
        help="explicitly permit forced resource shutdown; persistent terminals stay intact",
    )
    restart_p.set_defaults(func=_h_restart)


def _add_status_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_status

    status_p = sub.add_parser(
        "status",
        help="[host] one-screen view of sessions / pidfile / curl / infra / cron "
        "+ the gateway's own cluster-status snapshot",
    )
    status_p.set_defaults(func=_h_status)


def _add_converge_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_converge

    converge_p = sub.add_parser(
        "converge",
        help="[host] re-apply idempotent host wiring (symlink/PATH/dirs/plugin images/memory pool); "
        "normally run automatically by ava start",
    )
    converge_p.set_defaults(func=_h_converge)


def _add_firewall_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_firewall_status, _h_firewall_sync

    firewall_p = sub.add_parser(
        "firewall",
        help="macOS Application Firewall allowlist manifest (status / sync)",
    )
    firewall_sub = firewall_p.add_subparsers(dest="firewall_cmd", required=True)
    firewall_status_p = firewall_sub.add_parser(
        "status",
        help="audit the host + diff the allowlist manifest against ALF (read-only)",
    )
    firewall_status_p.set_defaults(func=_h_firewall_status)
    firewall_sync_p = firewall_sub.add_parser(
        "sync",
        help="apply the allowlist manifest now (rootless-first repair + prune; "
        "older macOS falls back to sudo -n/manual commands)",
    )
    firewall_sync_p.set_defaults(func=_h_firewall_sync)


def _h_lgtm(args: argparse.Namespace) -> int:
    from cli.commands import cmd_lgtm_off, cmd_lgtm_on, cmd_lgtm_status

    if args.lgtm_cmd == "on":
        return cmd_lgtm_on()
    if args.lgtm_cmd == "off":
        return cmd_lgtm_off()
    return cmd_lgtm_status()


def _add_lgtm_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # `ava lgtm on|off|status` — the observability-stack toggle on THIS host.
    # One command each way so observability's own overhead is measurable:
    # `off` removes the $AVA_HOME/lgtm-host marker (converge + watchdog stop
    # touching the stack) and compose-downs the containers (volumes persist);
    # `on` restores marker + stack with history intact.
    lgtm_p = sub.add_parser(
        "lgtm",
        help="observability stack (Loki/Grafana/Tempo/Prometheus) on/off/status on this host",
    )
    lgtm_sub = lgtm_p.add_subparsers(dest="lgtm_cmd", required=True)
    for name, help_text in (
        ("on", "designate this host as the LGTM host + bring the stack up (idempotent)"),
        ("off", "take the stack down + stop being the LGTM host (volumes persist)"),
        ("status", "marker + containers + readiness probes"),
    ):
        p = lgtm_sub.add_parser(name, help=help_text)
        p.set_defaults(func=_h_lgtm)


def _add_trace_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    from cli.main import _h_trace_ship

    # `ava trace ship` — replay the local OTel trace mirror to Tempo over OTLP
    trace_p = sub.add_parser("trace", help="trace mirror subcommands")
    trace_sub = trace_p.add_subparsers(dest="trace_cmd", required=True)
    trace_ship_p = trace_sub.add_parser(
        "ship",
        help="replay the local $AVA_HOME/traces mirror to Tempo over OTLP "
        "(incremental from a per-file watermark; --since/--until re-ships a window)",
    )
    trace_ship_p.add_argument(
        "--since",
        default=None,
        help="ship files dated on/after this day (YYYY-MM-DD); ignores watermark",
    )
    trace_ship_p.add_argument(
        "--until",
        default=None,
        help="ship files dated on/before this day (YYYY-MM-DD); ignores watermark",
    )
    trace_ship_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="count what would ship without POSTing",
    )
    trace_ship_p.set_defaults(func=_h_trace_ship)
