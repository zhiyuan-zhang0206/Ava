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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from base.host.env.registry import derived_env_keys, env_identity_keys
from base.native_process.os_platform import file_lock
from base.paths.home import home_checkout_error, resolve_ava_home
from base.paths.private_dir import ensure_private_dir
from base.telemetry import EventPipeline
from cli.start_identity import require_initialized
from cli.start_runtime import StartRuntime


def _checkout() -> Path:
    return Path(__file__).resolve().parents[1]


def _home() -> Path:
    """The home this start acts on: `$AVA_HOME`, else `~/.ava`.

    A home that carries its own `<home>/source` checkout starts only from that
    checkout (`base.paths.home.home_checkout_error`).
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


def run_start_command(**kwargs: Any) -> int:
    """`ava start`'s runtime delivery, imported on first use so admission stays settings-free."""
    from cli.commands.lifecycle.start import cmd_start

    return cmd_start(**kwargs)


def complete_start() -> None:
    """Report the root boot complete (lazy import, as above)."""
    from cli.commands.lifecycle.root_driver import complete_boot_start

    complete_boot_start()


def release_serving() -> None:
    """Withdraw the serving marker after a failed boot completion (lazy import, as above)."""
    from base.deploy.lifecycle.start_serving import clear_serving

    clear_serving()


def run_start(
    args: argparse.Namespace,
    *,
    runtime: StartRuntime | None = None,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
    database_factory: Callable[[], Any],
    producer: Callable[[], EventPipeline],
) -> int:
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")

    def start(*, boot: bool) -> int:
        return _start_once(
            args,
            runtime,
            retained_children,
            database_factory=database_factory,
            producer=producer,
            boot=boot,
        )

    if not args.boot:
        return start(boot=False)
    try:
        home = _home()
    except ValueError as exc:
        print(f"ava start: {exc}", file=sys.stderr)
        return 1
    return _run_boot_attempt(home, lambda: start(boot=True))


def _run_boot_attempt(home: Path, start: Callable[[], int]) -> int:
    """One boot-job start: a failed attempt asks to be retried until the budget is spent.

    The count never refuses a start (see `base.host.system.boot_policy`); it only
    decides what a *failed* attempt tells its scheduler.
    """
    from base.host.system import boot_policy

    attempt = boot_policy.begin_attempt(home)
    code = start()
    if code == 0:
        boot_policy.clear_attempts(home)
        return 0
    if not boot_policy.budget_spent(attempt):
        return code
    from base import telemetry

    print(
        f"ava start --boot: attempt {attempt} of {boot_policy.BOOT_MAX_ATTEMPTS} failed "
        f"(exit {code}); not retrying until an operator runs `ava start`",
        file=sys.stderr,
    )
    telemetry.emit(
        "telemetry",
        "boot_retries_exhausted",
        level="error",
        source="start",
        attributes={"attempts": attempt, "exit_code": code},
    )
    return boot_policy.exit_code_after_budget(code)


def _start_once(
    args: argparse.Namespace,
    runtime: StartRuntime | None,
    retained_children: list[subprocess.Popen[bytes]],
    *,
    database_factory: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    boot: bool = False,
) -> int:
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
            from base.log.process import init_cli_process
            from cli.main import _init_cli_logging

            def initialize(name: str) -> None:
                init_cli_process(
                    name=name, producer=producer, machine_reader=args.runtime.machine.name
                )

            _init_cli_logging(["start"], initialize=initialize)
            result = run_start_command(
                disabled_services=tuple(args.disable_service),
                only_services=tuple(args.only_service),
                all_services=args.all_services,
                runtime=runtime,
                retained_children=retained_children,
                database_factory=database_factory,
                producer=producer,
                command=args.runtime,
                boot_attempt=boot,
            )
            if result == 0:
                try:
                    complete_start()
                except (RuntimeError, OSError, TimeoutError):
                    release_serving()
                    raise
            return result
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        from pydantic import ValidationError

        if isinstance(exc, ValidationError):
            from cli.main import _print_settings_load_failure

            return _print_settings_load_failure(exc)
        print(f"ava start: {exc}", file=sys.stderr)
        return 1
