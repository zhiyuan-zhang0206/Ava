"""What the CLI entry and `ava start` share below both: which verbs open sinks, how a settings failure reads."""

from __future__ import annotations

import sys
from collections.abc import Callable

from pydantic import ValidationError

__all__ = ["init_cli_logging", "print_settings_load_failure"]

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


def init_cli_logging(args_in: list[str], *, initialize: Callable[[str], None]) -> None:
    """Open this verb's sinks, once its Settings may be built.

    Building them builds Settings, so `ava start` calls this only after the home
    is admitted, `ava init` opens none (it is Settings-free), and every other verb
    opens them after the home gates, right before dispatch. A Settings failure raised here is the one
    the command would raise, and reaches the same handlers.
    """
    name = _CLI_LOG_NAMES.get(tuple(args_in[:1])) or _CLI_LOG_NAMES.get(tuple(args_in[:2]))
    if name is not None:
        initialize(name)


def print_settings_load_failure(e: ValidationError) -> int:
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
