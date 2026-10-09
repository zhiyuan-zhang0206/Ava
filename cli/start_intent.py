"""Settings-free input boundary of ``ava start``.

Start admits a home `ava init` initialized; it takes no identity input of its own
(`cli/init_intent.py` does). Runtime Settings are imported only after the home is
admitted. The home and checkout resolution below is shared with `ava init`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from base.host.env.dotenv_boot import home_checkout_error, resolve_ava_home
from base.host.env.registry import derived_env_keys, env_identity_keys
from base.host.private_storage import ensure_private_dir
from base.native_process.os_platform import file_lock
from cli.start_identity import require_initialized
from cli.start_runtime import StartRuntime


def _checkout() -> Path:
    return Path(__file__).resolve().parents[1]


def _home() -> Path:
    """The home this start acts on: `$AVA_HOME`, else `~/.ava`.

    A home that carries its own `<home>/source` checkout starts only from that
    checkout (`base.host.env.dotenv_boot.home_checkout_error`).
    """
    error = home_checkout_error(_checkout())
    if error is not None:
        raise ValueError(error)
    return resolve_ava_home().resolve()


def _enter_home(home: Path) -> None:
    """Make this process the admitted home's: its `.env` is the only source of its
    identity, so inherited derived and identity keys never reach Settings."""
    for key in derived_env_keys() | env_identity_keys():
        os.environ.pop(key, None)
    os.environ["AVA_HOME"] = str(home)


def run_start(
    args: argparse.Namespace,
    *,
    runtime: StartRuntime | None = None,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    try:
        if runtime is None:
            runtime = StartRuntime.development(_checkout())
        home = _home()
        runtime.validate()
        # Refuse before anything is created: a start never makes a home, so the
        # lock below can rely on the directory existing.
        require_initialized(home)
        ensure_private_dir(home)
        with file_lock(home / "start-intent.lock", timeout_s=30):
            require_initialized(home)
            _enter_home(home)
            from cli.main import _init_cli_logging

            _init_cli_logging(["start"])
            from cli.commands.lifecycle.start import cmd_start

            result = cmd_start(
                disabled_services=tuple(args.disable_service),
                only_services=tuple(args.only_service),
                all_services=args.all_services,
                persist_services=args.persist_services,
                runtime=runtime,
                retained_children=retained_children,
            )
            if result == 0:
                from base.deploy.lifecycle.start_serving import clear_serving
                from cli.commands.lifecycle.root_driver import complete_boot_start

                try:
                    complete_boot_start()
                except (RuntimeError, OSError, TimeoutError):
                    clear_serving()
                    raise
            return result
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            from cli.main import _print_settings_load_failure

            return _print_settings_load_failure(exc)
        print(f"ava start: {exc}", file=sys.stderr)
        return 1
