"""`ava` CLI entry — argparse dispatch to the `cmd_*` implementations in `cli.commands`.

Registered in `pyproject.toml [project.scripts] ava = "cli.main:main"`; after
`uv sync`, `.venv/bin/ava` is callable. Ops layer only, decoupled from the
`ava.*` SDK — agent should not see cron / infra plumbing.

The argparse tree is built by `cli.parsers.build_parser` — one module per
command domain in `cli/parsers/` holding that domain's subcommand builders and
their `_h_*` handlers. This module re-exports the builders and every handler so
`cli.main` stays the namespace tests patch, and dispatch is a single
`args.func(args)` call. Importing `cli.commands` is deferred to the handler
bodies — that import triggers `Settings()` instantiation, which can
ValidationError on a fresh host with no ~/.ava/.env. main() catches that and
prints a copy-paste env template instead of a raw traceback.
"""

from __future__ import annotations

import os
import sys

from pydantic import ValidationError

# The parser tree and every `_h_*` handler live in cli/parsers/ (settings-free — they
# import cli.commands only inside handler bodies). Re-exported here so `cli.main` stays
# the namespace tests monkeypatch, and the `set_defaults(func=...)` bindings the
# builders make at parse time resolve through it.
from cli.parsers import build_parser as _build_parser
from cli.parsers.agents import (
    _h_agents_cancel,
    _h_agents_kill,
    _h_agents_ls,
    _h_agents_restart,
    _h_agents_resurrect,
    _h_agents_send,
    _h_agents_terminate,
    _h_agents_timeline,
    _h_notices_clear,
    _h_notices_list,
    _h_notices_resolve,
)
from cli.parsers.cluster import (
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
from cli.parsers.computer import _h_computer_release
from cli.parsers.host import (
    _h_converge,
    _h_firewall_status,
    _h_firewall_sync,
    _h_restart,
    _h_start,
    _h_status,
    _h_stop,
    _h_trace_ship,
)
from cli.parsers.impersonation import _h_impersonate, _h_impersonate_relay
from cli.parsers.logs import _h_logs_retention
from cli.parsers.management import (
    _h_config_get,
    _h_config_set,
    _h_config_unset,
    _h_presets_create,
    _h_presets_delete,
    _h_presets_get,
    _h_presets_ls,
    _h_presets_update,
    _h_schedules_create,
    _h_schedules_delete,
    _h_schedules_get,
    _h_schedules_logs,
    _h_schedules_ls,
    _h_schedules_provision,
    _h_schedules_restart,
    _h_schedules_runs,
    _h_schedules_start,
    _h_schedules_stop,
    _h_schedules_update,
)
from cli.parsers.mcp import (
    _h_mcp_add,
    _h_mcp_disable,
    _h_mcp_enable,
    _h_mcp_install,
    _h_mcp_list,
    _h_mcp_remove,
    _h_mcp_serve,
    _h_mcp_uninstall,
    _h_mcp_upgrade,
    _h_memory_init,
    _h_memory_refresh,
    _h_memory_search,
)
from cli.parsers.pitr import (
    _h_pitr_retention_inspect,
    _h_pitr_snapshot_archive,
    _h_pitr_snapshot_retire,
    _h_pitr_snapshot_verify,
)
from cli.parsers.plugins import (
    _h_plugins_disable,
    _h_plugins_enable,
    _h_plugins_inspect,
    _h_plugins_install,
    _h_plugins_installed,
    _h_plugins_uninstall,
    _h_plugins_update,
    _h_plugins_upgrade,
    _h_skill_disable,
    _h_skill_enable,
    _h_skill_install,
    _h_skill_register,
    _h_skill_scan,
    _h_skill_trust,
    _h_skill_update,
    _h_skill_upgrade,
)
from cli.parsers.pty import _h_pty_freeze, _h_pty_resume, _h_pty_status

__all__ = [
    "_h_agents_cancel",
    "_h_agents_kill",
    "_h_agents_ls",
    "_h_agents_restart",
    "_h_agents_resurrect",
    "_h_agents_send",
    "_h_agents_terminate",
    "_h_agents_timeline",
    "_h_cluster_cancel",
    "_h_cluster_destroy",
    "_h_cluster_down",
    "_h_cluster_ensure_db_role",
    "_h_cluster_health_probe",
    "_h_cluster_health_probe_register",
    "_h_cluster_health_probe_unregister",
    "_h_cluster_ls",
    "_h_cluster_pause",
    "_h_cluster_pitr_activate",
    "_h_cluster_pitr_rollback",
    "_h_cluster_pitr_status",
    "_h_cluster_recover",
    "_h_cluster_restart",
    "_h_cluster_resume",
    "_h_cluster_rollback",
    "_h_cluster_status",
    "_h_cluster_update",
    "_h_cluster_watchdog_probe",
    "_h_cluster_watchdog_probe_register",
    "_h_cluster_watchdog_probe_unregister",
    "_h_computer_release",
    "_h_config_get",
    "_h_config_set",
    "_h_config_unset",
    "_h_converge",
    "_h_firewall_status",
    "_h_firewall_sync",
    "_h_impersonate",
    "_h_impersonate_relay",
    "_h_logs_retention",
    "_h_mcp_add",
    "_h_mcp_disable",
    "_h_mcp_enable",
    "_h_mcp_install",
    "_h_mcp_list",
    "_h_mcp_remove",
    "_h_mcp_serve",
    "_h_mcp_uninstall",
    "_h_mcp_upgrade",
    "_h_memory_init",
    "_h_memory_refresh",
    "_h_memory_search",
    "_h_notices_clear",
    "_h_notices_list",
    "_h_notices_resolve",
    "_h_pitr_retention_inspect",
    "_h_pitr_snapshot_archive",
    "_h_pitr_snapshot_retire",
    "_h_pitr_snapshot_verify",
    "_h_plugins_disable",
    "_h_plugins_enable",
    "_h_plugins_inspect",
    "_h_plugins_install",
    "_h_plugins_installed",
    "_h_plugins_uninstall",
    "_h_plugins_update",
    "_h_plugins_upgrade",
    "_h_presets_create",
    "_h_presets_delete",
    "_h_presets_get",
    "_h_presets_ls",
    "_h_presets_update",
    "_h_pty_freeze",
    "_h_pty_resume",
    "_h_pty_status",
    "_h_restart",
    "_h_schedules_create",
    "_h_schedules_delete",
    "_h_schedules_get",
    "_h_schedules_logs",
    "_h_schedules_ls",
    "_h_schedules_provision",
    "_h_schedules_restart",
    "_h_schedules_runs",
    "_h_schedules_start",
    "_h_schedules_stop",
    "_h_schedules_update",
    "_h_skill_disable",
    "_h_skill_enable",
    "_h_skill_install",
    "_h_skill_register",
    "_h_skill_scan",
    "_h_skill_trust",
    "_h_skill_update",
    "_h_skill_upgrade",
    "_h_start",
    "_h_status",
    "_h_stop",
    "_h_trace_ship",
]

from shared.bootstrap import BootstrapFetchError
from shared.platform import ensure_line_buffered_stdio, ensure_utf8_stdio

# Force UTF-8 stdio on Windows before any status glyph is printed (a cp1252
# console raises UnicodeEncodeError on ✓/✗/→). No-op on POSIX. Also seeds
# PYTHONUTF8 for every child interpreter (birth subprocess, daemons, agents).
ensure_utf8_stdio()

# Line-buffer stdout so a long command piped into `tee` (every detached rollout /
# updater session) streams its own progress in real time instead of block-buffering
# it to the end of the log, out of order against its children's unbuffered output.
ensure_line_buffered_stdio()

# Detached CLI invocations (gateway `spawn_update` child / the ops update-trigger
# `ops.controllers.update_trigger.trigger_update`, run by the watchdog's schema/pin
# controllers) set `AVA_CLI_LOG_NAME` in the child env. When set,
# we wire the same loguru sinks the long-running daemons use so the
# child's logged steps land in `~/.ava/logs/<name>.log` + PG
# `events` (queryable via `/api/cluster/admin/events`). Without
# this, a stuck `ava cluster update` child's last words live only in the
# parent's per-invocation Popen log file, never reaching the cluster
# admin events surface.
#
# Interactive CLI use leaves the env unset and skips init — the
# extra sinks would write one PG row per `ava status` invocation.
_cli_log_name = os.environ.get("AVA_CLI_LOG_NAME")
if _cli_log_name:
    from shared.log import init_cli_process

    init_cli_process(name=_cli_log_name)


# Settings-lite verbs — they must construct Settings while the gateway is down
# (a runner's stop / status / watchdog-probe are its recovery path), so
# `cli.main` opts them out of the gateway config fetch that every other process
# performs at Settings build. The fetch decision itself is role-derived
# (shared.bootstrap.config_source_is_local; AVA_CONFIG_SOURCE is gone).
_LITE_VERBS = frozenset(
    {
        "stop",
        # `restart` is deliberately NOT here: its preflight registers this
        # machine in the central DB and its start leg needs the cluster config
        # regardless, so on a pure runner a lite restart could only ever hit
        # the never-dialed placeholder DB URL (UnanchoredHomeError, observed on
        # the fleet Windows box). With the gateway down, restart now fails at
        # the fetch with the actionable BootstrapFetchError — `stop` stays lite
        # and remains the recovery verb.
        "status",
        "pty",
        "cluster",
        "agents",
        "config",
        "presets",
        "schedules",
        "logs",
        "mcp",
        "memory",
        "plugins",
        "skill",
        "enroll",
        "boot",
    }
)


# Verbs that act on THIS checkout's own cluster and name no target. An unanchored
# checkout resolves to the default home (production), so these must refuse rather than
# reach it — `cli.preflight.require_anchored_home`. `start` is gated separately, by the
# stricter installed-home check.
#
# The membership rule is mechanical: a verb belongs here iff it acts on the current
# home AND takes no explicit target. That is why `cluster down` / `cluster destroy` are
# absent — both REQUIRE `--path` and address a home by name, so they never act on the
# current one; and why the read-only (`ls`, `status`) and probe-registration
# subcommands are absent too.
_ANCHORED_HOME_VERBS = frozenset({"stop", "restart", "converge", "logs", "maintenance"})
_ANCHORED_HOME_CLUSTER_SUBVERBS = frozenset(
    {"update", "restart", "rollback", "recover", "cancel", "ensure-db-role", "ensure-runner-role"}
)


def _print_settings_load_failure(e: ValidationError) -> int:
    """Translate Pydantic settings ValidationError into a copy-paste env template.

    Hit on a fresh host (no ~/.ava/.env, or missing required fields like
    AVA_DB_URL / AVA_REDIS_URL). `ava enroll` writes these; this prints the
    minimum env template for the manual path.
    """
    missing: list[str] = []
    for err in e.errors():
        loc = err.get("loc", ())
        if not loc:
            continue
        token = loc[0]
        if isinstance(token, str):
            # Settings fields all use explicit aliases like `alias="AVA_X"`,
            # but Pydantic's error loc reports the Python attribute name.
            # Convert by uppercasing + AVA_ prefix; matches every alias today.
            missing.append(token if token.startswith("AVA_") else f"AVA_{token.upper()}")
    print(
        "\n✗ ava: failed to load settings — ~/.ava/.env is missing required fields.\n",
        file=sys.stderr,
    )
    if missing:
        print("Missing env vars:", file=sys.stderr)
        for var in missing:
            print(f"  {var}=<value>", file=sys.stderr)
    print(
        "\nAdd the lines above to ~/.ava/.env, then re-run your command. For the full\n"
        "agent-runner bring-up flow, use `ava enroll --gateway <url> --machine-name <name> "
        "--machine-host <this-host-addr>`.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    os.environ.pop("AVA_PROCESS_PROFILE", None)
    # The CLI is a settings-full process and must never inherit a launcher-set
    # process profile: with no marker, profiles.py constructs every domain as before.
    # Importing shared.config.profiles initializes shared.config first, so that
    # constant cannot be used before this cleanup without constructing Settings.
    # `ava enroll` bootstraps a fresh agent-runner that has no full config yet, so
    # it must run BEFORE any cli.commands import (handlers defer that import, but
    # parser building doesn't need it either — argparse builds fine without
    # Settings()). It lives in a settings-free module that does not import
    # shared.config. (Host provisioning is `scripts/install.sh`, not a CLI verb.)
    args_in = sys.argv[1:] if argv is None else argv
    if args_in and args_in[0] == "enroll":
        from cli.enroll import run_enroll

        return run_enroll(args_in[1:])

    # `ava boot` is what the OS boot job runs on the platforms whose scheduler
    # cannot retry a failed job for us (Linux cron `@reboot`, Windows ONLOGON):
    # `ava start` re-run while the machine is still coming up. Dispatched here,
    # before the settings-gated import, so it can retry a start that failed for
    # ANY reason — a settings error included. See shared/boot_policy.py.
    if args_in and args_in[0] == "boot":
        from cli.boot_retry import run_boot

        return run_boot(args_in[1:])

    # Maintenance verbs build settings-lite: no gateway fetch, so they still work
    # while the gateway is down (a runner's stop / status / watchdog-probe are its
    # recovery path). Every other verb — start, converge, update, trace-ship — and
    # every daemon/agent process fetches per its own role at Settings build.
    # shared.session_env does not forward this var, so processes a lite verb spawns
    # never inherit the opt-out.
    if args_in and args_in[0] in _LITE_VERBS:
        os.environ.setdefault("AVA_CONFIG_FETCH", "skip")
    if args_in[:2] in (
        ["maintenance", "status"],
        ["maintenance", "stop"],
        ["maintenance", "stop-data-plane"],
    ):
        os.environ.setdefault("AVA_CONFIG_FETCH", "skip")

    # Home gates — settings-free (cli.preflight), so an uninstalled/unanchored home
    # gets the actionable pointer instead of the generic Settings validation error the
    # cli.commands import would raise first. Skips --help (parse-only invocations).
    #
    # `start` takes the full installed-home check (registry record + port block: it is
    # about to bring a data plane UP). Every other verb that acts on THIS checkout's
    # cluster takes the anchoring check alone — enough to stop an unanchored dev
    # worktree from reaching production, without blocking a home whose registry record
    # is gone from cleaning itself up.
    if args_in and not ({"-h", "--help"} & set(args_in)):
        verb = args_in[0]
        sub = args_in[1] if len(args_in) > 1 else ""
        if verb == "start" or (verb == "maintenance" and sub == "start"):
            from cli.preflight import require_installed_home

            rc = require_installed_home()
            if rc is not None:
                return rc
        elif verb in _ANCHORED_HOME_VERBS or (
            verb == "cluster" and sub in _ANCHORED_HOME_CLUSTER_SUBVERBS
        ):
            from cli.preflight import require_anchored_home

            rc = require_anchored_home(f"{verb} {sub}" if verb == "cluster" else verb)
            if rc is not None:
                return rc

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValidationError as exc:
        return _print_settings_load_failure(exc)
    except BootstrapFetchError as exc:
        # A pure agent-runner whose gateway is unreachable (or which was never
        # enrolled) fails fast with an actionable message; `ava boot` retries.
        print(f"✗ ava: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
