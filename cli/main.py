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
from pathlib import Path

from pydantic import ValidationError

from base.host.env.bootstrap import BootstrapFetchError
from base.host.env.dotenv_boot import LAUNCHER_PROFILE_ENV_KEY
from base.native_process import code_version
from base.native_process.os_platform import (
    LockTimeoutError,
    ensure_line_buffered_stdio,
)

# The parser tree and every `_h_*` handler live in cli/parsers/ (settings-free — they
# import cli.commands only inside handler bodies). Only the builder entry point is
# imported here; dispatch after parsing is `args.func(args)`, resolved by the
# `set_defaults(func=...)` bindings the builders made against their own module globals.
from cli.parsers import build_parser as _build_parser

# Line-buffer stdout so a long command piped into `tee` (every detached rollout /
# updater session) streams its own progress in real time instead of block-buffering
# it to the end of the log, out of order against its children's unbuffered output.
ensure_line_buffered_stdio()


# The verbs that bring this unit up (every in-process `cmd_start`) open the
# loguru sinks a service process has, under these names. Importing `base.log`
# drops loguru's default handler, so without them every record the start path
# writes only through loguru is discarded: a skipped pgvector pre-create,
# untracked migration files that will not be applied. `init_cli_process` adds
# stderr, `$AVA_HOME/logs/<name>.log` and the event pipeline, and emits no
# `service_started` row. Other verbs print to the caller's terminal and open
# none: the event pipeline would carry one row per `ava status`.
_CLI_LOG_NAMES: dict[tuple[str, ...], str] = {
    ("start",): "cli-start",
    ("restart",): "cli-restart",
    ("lgtm", "on"): "cli-lgtm",
    ("lgtm", "off"): "cli-lgtm",
}


def _init_cli_logging(args_in: list[str]) -> None:
    """Open this verb's sinks, once its Settings may be built.

    Building them builds Settings, so `ava start` calls this only after the home
    is admitted, `ava init` opens none (it is Settings-free), and every other verb
    opens them after the home gates, right before dispatch. A Settings failure raised here is the one
    the command would raise, and reaches the same handlers.
    """
    name = _CLI_LOG_NAMES.get(tuple(args_in[:1])) or _CLI_LOG_NAMES.get(tuple(args_in[:2]))
    if name is not None:
        from base.log import init_cli_process

        init_cli_process(name=name)


# Settings-lite verbs — they must construct Settings while the gateway is down
# (stop and status must remain available for recovery inspection), so
# `cli.main` opts them out of the gateway config fetch that every other process
# performs at Settings build. The fetch decision itself is role-derived
# (base.host.env.bootstrap.config_source_is_local; AVA_CONFIG_SOURCE is gone).
_LITE_VERBS = frozenset(
    {
        "stop",
        # `restart` is deliberately NOT here: its preflight registers this
        # machine in the central DB and its start leg needs the cluster config
        # regardless, so on a pure runner a lite restart could only ever hit
        # the never-dialed placeholder DB URL (PlaceholderDbUrlError, observed on
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
    }
)


def _print_settings_load_failure(e: ValidationError) -> int:
    """Translate Pydantic settings ValidationError into a copy-paste env template.

    Hit on a fresh host (no ~/.ava/.env, or missing required fields like
    AVA_DB_URL / AVA_REDIS_URL). `ava start` resolves these; this prints the
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
        "agent-runner bring-up flow, use `ava init --serve-agent-runner --no-serve-gateway --gateway-url <url> --machine-name <name> "
        "--machine-host <this-host-addr> --db-capability <bundle>`, then `ava start`.",
        file=sys.stderr,
    )
    return 1


def _normalize_process_profile() -> None:
    """Pop the launcher's process profile, recording it for the boot pass.

    The CLI is a settings-full process and must never inherit a launcher-set
    process profile: with no marker, profiles.py constructs every domain as
    before. Importing base.config.profiles initializes base.config first,
    so that constant cannot be used before this cleanup without constructing
    Settings. First start resolves and persists unit identity before importing
    cli.commands; parser construction remains settings-free.

    The popped value is recorded, not discarded: an agent-launched tree keeps
    the launcher's injected runner DB / Redis projections through the authority
    pass only under a live-or-recorded agent profile (base/host/env/dotenv_boot.py
    `_enforce_cluster_env_authority`), so popping without recording made that
    exemption unreachable on every CLI path — `ava cluster health-probe` run
    from an agent child on a pure agent-runner fell back to the sentinel
    (#4334). The record is never cleared: a nested CLI overwrites it only when
    it carries a fresh live marker of its own.
    """
    launcher_profile = os.environ.pop("AVA_PROCESS_PROFILE", None)
    if launcher_profile:
        os.environ[LAUNCHER_PROFILE_ENV_KEY] = launcher_profile


def _opt_into_lite_config(args_in: list[str]) -> None:
    """Verbs that must run without the config fetch build settings-lite instead."""
    # `maintenance status` builds settings-lite: it only reads the local journal.
    # `repair` and `cancel` dial this unit's real database, so they fetch like
    # every other verb -- start, converge, update, trace-ship -- and every
    # daemon/agent process fetches per its own role at Settings build.
    # base.sessions.env_forwarding does not forward this var, so processes a
    # lite verb spawns never inherit the opt-out.
    from cli.preflight import unit_already_stopped

    if (
        args_in
        and args_in[0] in _LITE_VERBS
        and (args_in[0] != "stop" or "--force" in args_in or unit_already_stopped())
    ):
        os.environ.setdefault("AVA_CONFIG_FETCH", "skip")
    if args_in[:2] == ["maintenance", "status"]:
        os.environ.setdefault("AVA_CONFIG_FETCH", "skip")


def main(argv: list[str] | None = None) -> int:
    # The operator CLI is exempt from the database code-version gate: `ava stop`
    # writes to drain agents, so a host left behind by an update must still be able
    # to run it. Service processes are launched with `python -m <module>`, never
    # through here, so every one of them stays gated. Declared first, before any
    # verb can dial the database.
    code_version.exempt_from_db_gate()
    _normalize_process_profile()
    args_in = sys.argv[1:] if argv is None else argv
    # The checkout gate comes first, before `boot` and before anything loads a
    # home's configuration: every command is refused unless this CLI belongs to
    # the home (`cli.preflight.require_own_checkout`).
    from cli.preflight import require_own_checkout

    rc = require_own_checkout(args_in, Path(__file__).resolve().parents[1])
    if rc is not None:
        return rc
    # `ava boot` is what the OS boot job runs on the platforms whose scheduler
    # cannot retry a failed job for us (Linux cron `@reboot`, Windows ONLOGON):
    # `ava start` re-run while the machine is still coming up. Dispatched here,
    # before the settings-gated import, so it can retry a start that failed for
    # ANY reason — a settings error included. See base/host/system/boot_policy.py.
    if args_in and args_in[0] == "boot":
        from cli.boot_retry import run_boot

        return run_boot(args_in[1:])

    _opt_into_lite_config(args_in)

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args_in[:1] not in (["init"], ["start"]):  # these two open their own, after admission
            _init_cli_logging(args_in)
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
