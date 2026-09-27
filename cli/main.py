"""`ava` CLI entry — argparse dispatch to the `cmd_*` implementations in `cli.commands`.

Registered in `pyproject.toml [project.scripts] ava = "cli.main:main"`; after
`uv sync`, `.venv/bin/ava` is callable. Ops layer only, decoupled from the
`ava.*` SDK — agent should not see cron / infra plumbing.

The argparse tree is built by `cli.parsers.build_parser` — one module per
command domain in `cli/parsers/` holding that domain's subcommand builders and
their `_h_*` handlers. A builder binds its own module's handler directly
(`set_defaults(func=_h_x)`, referring to the function defined earlier in that
same module) — no registry, no re-export. Dispatch is a single
`args.func(args)` call. A test that fakes a handler patches the parser module
that defines it (`monkeypatch.setattr(cli.parsers.<domain>, "_h_x", ...)`)
*before* `build_parser()` runs: the builder reads the module global by name at
build time, so a patch applied after the tree is built never takes effect.
Importing `cli.commands` is deferred to the handler bodies — that import
triggers `Settings()` instantiation, which can ValidationError on a fresh host
with no ~/.ava/.env. main() catches that and prints a copy-paste env template
instead of a raw traceback.
"""

from __future__ import annotations

import os
import sys
from contextlib import suppress

from pydantic import ValidationError

# The parser tree and every `_h_*` handler live in cli/parsers/ (settings-free — they
# import cli.commands only inside handler bodies). Only the builder entry point is
# imported here; dispatch after parsing is `args.func(args)`, resolved by the
# `set_defaults(func=...)` bindings the builders made against their own module globals.
from cli.parsers import build_parser as _build_parser
from shared.bootstrap import BootstrapFetchError
from shared.platform import LockTimeoutError, ensure_line_buffered_stdio, ensure_utf8_stdio

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
def _init_detached_cli_logging() -> None:
    """Initialize detached-command sinks only after settings-free dispatch.

    Importing ``cli.main`` must remain side-effect free for retained-image and
    bootstrap entries that select their settings-free path inside ``main``.
    """
    cli_log_name = os.environ.get("AVA_CLI_LOG_NAME")
    if cli_log_name:
        from shared.log import init_cli_process

        # The updater's recovery child must remain usable while the gateway or DB is
        # restarting; stderr/file sinks are still useful if the Postgres sink fails.
        with suppress(Exception):
            init_cli_process(name=cli_log_name)


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
_ANCHORED_HOME_VERBS = frozenset({"stop", "pause", "restart", "converge", "logs", "maintenance"})
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


# Where the recorded launcher profile lives; shared/dotenv_boot.py reads the
# same key back (`LAUNCHER_PROFILE_ENV_KEY`). It stays a literal here:
# importing shared.dotenv_boot at CLI entry is not safe — it resolves the
# process home at import (resolve_ava_home raises for an installed wheel
# without an explicit absolute AVA_HOME, and on an env/checkout home
# contradiction), while `ava enroll` must run exactly on hosts where those
# gates cannot hold yet.
_LAUNCHER_PROFILE_ENV_KEY = "AVA_LAUNCHER_PROFILE"


def _normalize_process_profile() -> None:
    """Pop the launcher's process profile, recording it for the boot pass.

    The CLI is a settings-full process and must never inherit a launcher-set
    process profile: with no marker, profiles.py constructs every domain as
    before. Importing shared.config.profiles initializes shared.config first,
    so that constant cannot be used before this cleanup without constructing
    Settings. `ava enroll` bootstraps a fresh agent-runner that has no full
    config yet, so it must run BEFORE any cli.commands import (handlers defer
    that import, but parser building doesn't need it either — argparse builds
    fine without Settings()). It lives in a settings-free module that does not
    import shared.config. (Host provisioning is `scripts/install.sh`, not a
    CLI verb.)

    The popped value is recorded, not discarded: an agent-launched tree keeps
    the launcher's injected runner DB / Redis projections through the authority
    pass only under a live-or-recorded agent profile (shared/dotenv_boot.py
    `_enforce_cluster_env_authority`), so popping without recording made that
    exemption unreachable on every CLI path — `ava cluster health-probe` run
    from an agent child on a pure agent-runner fell back to the sentinel
    (#4334). The record is never cleared: a nested CLI overwrites it only when
    it carries a fresh live marker of its own.
    """
    launcher_profile = os.environ.pop("AVA_PROCESS_PROFILE", None)
    if launcher_profile:
        os.environ[_LAUNCHER_PROFILE_ENV_KEY] = launcher_profile


def main(argv: list[str] | None = None) -> int:
    _normalize_process_profile()
    args_in = sys.argv[1:] if argv is None else argv
    if args_in[:2] == ["cluster", "update"]:
        # A verified wheel has no checkout anchor. Parse the SAME public tree,
        # then verify the loaded image/home before importing settings-full ops.
        # Parse first instead of scanning raw tokens so argparse abbreviations
        # cannot make a prepared entry fall through to ordinary source dispatch.
        args = _build_parser().parse_args(args_in)
        if getattr(args, "prepared", None) is not None:
            return args.func(args)

    _init_detached_cli_logging()
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

    # Maintenance verbs build settings-lite only for `status` (journal-only) and
    # `stop-data-plane` (a gateway-role verb, where the fetch is local anyway).
    # `maintenance stop` deliberately does NOT: its verify-drained and
    # host-identity legs dial this unit's real data plane, so on a pure runner a
    # lite stop could only ever hit the never-dialed placeholder DB URL
    # (UnanchoredHomeError — the 2026-09-13 drill stall, issue #2346). With the
    # gateway down such a stop now fails at the fetch with the actionable
    # BootstrapFetchError — the same contract `restart` took above. Every other
    # verb — start, converge, update, trace-ship — and every daemon/agent process
    # fetches per its own role at Settings build. shared.session_env does not
    # forward this var, so processes a lite verb spawns never inherit the opt-out.
    from cli.preflight import unit_already_stopped

    if (
        args_in
        and args_in[0] in _LITE_VERBS
        and (args_in[0] != "stop" or "--force" in args_in or unit_already_stopped())
    ):
        os.environ.setdefault("AVA_CONFIG_FETCH", "skip")
    if args_in[:2] in (
        ["maintenance", "status"],
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
    except LockTimeoutError as exc:
        print(
            f"ava: another local lifecycle operation is active; retry after it finishes: {exc}",
            file=sys.stderr,
        )
        return 1
    except ValidationError as exc:
        return _print_settings_load_failure(exc)
    except BootstrapFetchError as exc:
        # A pure agent-runner whose gateway is unreachable (or which was never
        # enrolled) fails fast with an actionable message; `ava boot` retries.
        print(f"✗ ava: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
