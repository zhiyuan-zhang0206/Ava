"""Settings-free gate for the CLI: which checkout may act on a home.

First start publishes identity through cli.start_intent before Settings loads.
Every `ava` invocation passes `require_own_checkout` first.
"""

from __future__ import annotations

import sys
from pathlib import Path

from base.deploy.maintenance.state import MaintenancePhase


def require_own_checkout(args_in: list[str], repo: Path) -> int | None:
    """Refuse every command from a checkout that is not the home's own.

    A home that carries its own `<home>/source` checkout is operated only by that
    checkout's CLI (`base.host.env.dotenv_boot.home_checkout_error`); a home with
    none (a test home) accepts any checkout. There is no read-only exemption: a
    development CLI that needs a home names a temporary `AVA_HOME`. The one pass is
    an argv of nothing or of a lone `-h`/`--help`, which only parses and never
    reaches a verb. Returns None to proceed, an error rc to refuse.

    `repo` is the checkout the running CLI belongs to. This gate is settings-free
    and runs before every other one, so a foreign checkout is stopped before
    anything loads the home's configuration. The refusal does not echo the argv: it
    can carry a secret (`ava config set KEY=...`).
    """
    from base.host.env.dotenv_boot import home_checkout_error

    if args_in in ([], ["-h"], ["--help"]):
        return None
    error = home_checkout_error(repo)
    if error is None:
        return None
    print(f"✗ ava: {error}", file=sys.stderr)
    return 1


def unit_already_stopped() -> bool:
    """Allow an idempotent cold stop without fetching the offline gateway."""
    from base.deploy.maintenance.pause_owner import read_for_home
    from base.host.env.dotenv_boot import resolve_ava_home

    current = read_for_home(resolve_ava_home())
    return (
        current.status == "paused"
        and current.maintenance is not None
        and current.maintenance.phase == MaintenancePhase.STOPPED
        and not current.maintenance.failures
    )
