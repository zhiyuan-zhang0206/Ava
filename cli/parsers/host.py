"""`ava` host-level lifecycle verbs — argparse builders + their `_h_*` handlers.

`init` / `start` / `stop` / `restart` / `status` / `converge` / `firewall` / `trace` /
`lgtm` act
on THIS host (or the unit this checkout owns), as opposed to the cluster-wide
verbs in ``cli.parsers.cluster``. Handlers stay thin: each lazy-imports its
`cmd_*` implementation from ``cli.commands`` so building the parser never loads
Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse
import subprocess
from functools import partial


def _h_init(args: argparse.Namespace) -> int:
    from cli.init_intent import run_init

    return run_init(args)


def _h_start(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    from cli.start_intent import run_start

    return run_start(
        args,
        retained_children=retained_children,
        database_factory=args.database_factory,
        producer=args.producer,
    )


def _h_stop(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    from cli.commands.lifecycle.stop import cmd_stop

    return cmd_stop(
        keep_infra=args.keep_infra,
        require_confirmation=not args.yes,
        preserve_sessions=frozenset(args.keep_service),
        force=args.force,
        timeout=args.timeout,
        retained_children=retained_children,
        database_factory=args.database_factory,
        producer=args.producer,
    )


def _h_restart(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    from cli.commands.lifecycle.stop import cmd_restart

    return cmd_restart(
        mode=args.mode,
        retained_children=retained_children,
        database_factory=args.database_factory,
        producer=args.producer,
    )


def _h_status(args: argparse.Namespace) -> int:
    if args.json:
        from cli.commands.lifecycle.hold_report import cmd_status_json

        return cmd_status_json()
    from cli.commands.lifecycle.status import cmd_status

    return cmd_status(database_factory=args.database_factory)


def _h_converge(_args: argparse.Namespace) -> int:
    from cli.commands.converge.host import cmd_converge

    return cmd_converge(database_factory=_args.database_factory, producer=_args.producer)


def _h_firewall_status(_args: argparse.Namespace) -> int:
    from cli.commands.converge.firewall_command import cmd_firewall_status

    return cmd_firewall_status()


def _h_firewall_sync(_args: argparse.Namespace) -> int:
    from cli.commands.converge.firewall_command import cmd_firewall_sync

    return cmd_firewall_sync()


def _h_trace_ship(args: argparse.Namespace) -> int:
    from cli.commands.observability.trace import cmd_trace_ship

    return cmd_trace_ship(since=args.since, until=args.until, dry_run=args.dry_run)


def _add_init_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # `ava init` — the home's identity, recorded once. NO TTY prompt — agent-first
    # design, agent has no TTY, missing values fail loud. Settings-free: it starts
    # nothing; the first `ava start` provisions the data plane and launches.
    init_p = sub.add_parser(
        "init",
        help="[host] record this home's identity once (machine name, capabilities, "
        "credentials, ports) before its first `ava start`; starts nothing. The home is "
        "$AVA_HOME, else ~/.ava — never a flag. A home that is already initialized is "
        "refused; `ava start` brings it up.",
    )
    init_p.add_argument(
        "--machine-name",
        default=None,
        help="stable identifier for this host (e.g. host-a / host-b); required",
    )
    init_p.add_argument(
        "--serve-gateway",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="serve the gateway capability (central pg/redis + all daemons). A single box "
        "passes both --serve-gateway --serve-agent-runner",
    )
    init_p.add_argument(
        "--serve-agent-runner",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="serve the agent-runner capability (agent-host/ops/watchdog)",
    )
    init_p.add_argument(
        "--serve-observability-station",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="serve the observability-station capability (own the native LGTM "
        "observability backends — the declarative form of the $AVA_HOME/lgtm-host marker)",
    )
    init_p.add_argument(
        "--machine-description",
        default=None,
        help='free-text note of what this host is for (e.g. "voice IO + browser")',
    )
    init_p.add_argument(
        "--memory-remote",
        default=None,
        help="central git remote URL for memory pool (e.g. git@github.com:you/AvaMemory.git)",
    )
    init_p.add_argument(
        "--gateway-url",
        default=None,
        help="public URL of the gateway. On the gateway this host's own URL; on an "
        "agent-runner, the gateway it reaches (required there)",
    )
    init_p.add_argument(
        "--config-file",
        type=str,
        default=None,
        help="explicit dotenv configuration, outside the home",
    )
    init_p.add_argument(
        "--machine-host", default=None, help="this host's reachable private-network address"
    )
    init_p.add_argument("--ssl-cert-file", default=None, help="CA bundle for gateway verification")
    init_p.add_argument(
        "--db-capability",
        default=None,
        metavar="BUNDLE",
        help="agent-runner only: install the database capability bundle the gateway operator "
        "issued (`ava cluster db-authority issue-unit`); its transport key comes from "
        "AVA_DB_CAPABILITY_KEY. The bundle file is deleted once installed. A later bundle "
        "goes to `ava cluster db-authority install-unit`.",
    )
    init_p.set_defaults(func=_h_init)


def _add_start_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> None:
    # `ava start` — brings up a home `ava init` initialized; the service selection
    # is its only input.
    start_p = sub.add_parser(
        "start",
        help="[host] bring up this unit's full stack (idempotent). The home is "
        "$AVA_HOME, else ~/.ava — never a flag. It must be initialized first: "
        "`ava init` takes the machine identity (name, capabilities, gateway).",
    )
    selection = start_p.add_mutually_exclusive_group()
    selection.add_argument(
        "--disable-service",
        action="append",
        default=[],
        metavar="SERVICE",
        help="durably disable this service session (repeatable; e.g. --disable-service labeler "
        "--disable-service frontend). Pass the bare service name. Bare `ava start` preserves "
        "this selection; use `--all-services` to reset it or `--only-service` to replace it.",
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
    selection.add_argument(
        "--all-services", action="store_true", help="explicitly select the entire roster"
    )
    selection.add_argument(
        "--only-service",
        action="append",
        default=[],
        metavar="SERVICE",
        help="run only these services (repeatable; persisted for restart)",
    )
    start_p.set_defaults(
        func=_h_start
        if retained_children is None
        else partial(_h_start, retained_children=retained_children)
    )


def _add_stop_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> None:
    stop_p = sub.add_parser(
        "stop",
        help="[host] stop services, terminals and data plane; preserve data and agent identities",
    )
    stop_p.add_argument(
        "--keep-infra",
        action="store_true",
        help="do not stop THIS cluster's own Postgres/Redis instance (every "
        "cluster owns one, under its $AVA_HOME). Used by `ava restart`, whose start "
        "leg still needs the database. A plain `ava stop` means 'fully stop' and "
        "leaves this off.",
    )
    stop_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip the stdin y/N confirmation (non-interactive / scripted use).",
    )
    stop_p.add_argument(
        "--keep-service",
        action="append",
        default=[],
        metavar="SERVICE",
        help="retain a named service (repeatable); DB-dependent services require --keep-infra",
    )
    # task #4092 cli-default inventory: bounded graceful drain — "failure never
    # silently forces" makes the default safe, and ops scripts rely on it.
    stop_p.add_argument(
        "--timeout",
        type=float,
        default=300,
        help="total normal drain/stop deadline; failure never silently forces",
    )
    stop_p.add_argument(
        "--force",
        action="store_true",
        help="explicitly permit force-killing work that cannot exit normally",
    )
    stop_p.set_defaults(
        func=_h_stop
        if retained_children is None
        else partial(_h_stop, retained_children=retained_children)
    )


def _add_restart_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> None:
    restart_p = sub.add_parser(
        "restart",
        help="[host] normal stop then start, retaining the data plane and browser (persistent terminals close, as at stop)",
    )
    # task #4092 cli-default inventory: "smooth" is the safe default — force
    # must be asked for explicitly.
    restart_p.add_argument(
        "--mode",
        choices=("smooth", "force"),
        default="smooth",
        help="'smooth' preserves completed work; 'force' explicitly permits forced resource shutdown",
    )
    restart_p.set_defaults(
        func=_h_restart
        if retained_children is None
        else partial(_h_restart, retained_children=retained_children)
    )


def _add_status_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    status_p = sub.add_parser(
        "status",
        help="[host] one-screen view of sessions / pidfile / curl / infra / cron "
        "+ the gateway's own cluster-status snapshot",
    )
    status_p.add_argument(
        "--json",
        action="store_true",
        help="print only this unit's maintenance hold as one JSON object "
        "(reads the local journal; probes nothing)",
    )
    status_p.set_defaults(func=_h_status)


def _add_converge_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    converge_p = sub.add_parser(
        "converge",
        help="[host] re-apply idempotent host wiring (symlink/PATH/dirs/plugin images/memory pool); "
        "normally run automatically by ava start",
    )
    converge_p.set_defaults(func=_h_converge)


def _add_firewall_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
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


def _h_lgtm(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    from cli.commands.observability.lgtm import cmd_lgtm_off, cmd_lgtm_on, cmd_lgtm_status

    if args.lgtm_cmd == "on":
        return cmd_lgtm_on(
            retained_children=retained_children,
            database_factory=args.database_factory,
            producer=args.producer,
        )
    if args.lgtm_cmd == "off":
        return cmd_lgtm_off(
            retained_children=retained_children,
            database_factory=args.database_factory,
            producer=args.producer,
        )
    if args.lgtm_cmd == "render":
        from cli.commands.observability.grafana_render import cmd_grafana_render

        return cmd_grafana_render(
            force=args.force, repo_only=args.repo_only, database_factory=args.database_factory
        )
    return cmd_lgtm_status()


def _add_lgtm_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> None:
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
        p.set_defaults(
            func=_h_lgtm
            if retained_children is None or name == "status"
            else partial(_h_lgtm, retained_children=retained_children)
        )
    render_p = lgtm_sub.add_parser(
        "render",
        help="render the ava-ops dashboard from the metric registries and diff it "
        "against this host's provisioning copy (--force writes the render)",
    )
    render_p.add_argument(
        "--force",
        action="store_true",
        help="write the render into this host's provisioning tree (atomic; the "
        "diff preview is the default)",
    )
    render_p.add_argument(
        "--repo-only",
        action="store_true",
        help="skip the installed-plugin registry read — render the checkout's "
        "plugins only (for a preview on a host without a reachable database)",
    )
    render_p.set_defaults(func=_h_lgtm)


def _add_trace_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
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
