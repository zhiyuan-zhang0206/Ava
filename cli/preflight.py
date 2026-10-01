"""Settings-free gate for lifecycle commands: which checkout may act on a home.

First start publishes identity through cli.start_intent before Settings loads.
Every verb that changes the resolved home passes `require_own_checkout` first.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Verbs that read and change nothing on the home they resolve; any checkout may
# run them. A verb missing from this list is refused from a foreign checkout:
# the list is the only way a verb becomes exempt.
_READ_ONLY_VERBS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("status",),
        ("maintenance", "status"),
        ("pty", "status"),
        ("firewall", "status"),
        ("lgtm", "status"),
        ("cluster", "status"),
        ("agents", "ls"),
        ("agents", "timeline"),
        ("notices", "list"),
        ("impersonate", "list"),
        ("impersonate", "status"),
        ("config", "get"),
        ("config", "audit"),
        ("presets", "ls"),
        ("presets", "get"),
        ("schedules", "ls"),
        ("schedules", "get"),
        ("schedules", "runs"),
        ("schedules", "logs"),
        ("plugins", "installed"),
        ("plugins", "inspect"),
        ("mcp", "list"),
        ("mcp", "ls"),
        ("memory", "search"),
        ("packages", "status"),
        ("backup", "operations", "status"),
        ("pitr", "retention", "inspect"),
        ("pitr", "retention", "status"),
        ("pitr", "multipart", "list"),
        ("pitr", "snapshot", "verify"),
    }
)


def _command_path(args_in: list[str]) -> tuple[str, ...]:
    """The leading verb words of an argv (up to three), stopping at the first flag."""
    path: list[str] = []
    for token in args_in:
        if token.startswith("-"):
            break
        path.append(token)
    return tuple(path[:3])


def require_own_checkout(args_in: list[str], repo: Path) -> int | None:
    """Refuse a verb that changes a home from a checkout that is not the home's own.

    A home that carries its own `<home>/source` checkout is changed only by that
    checkout's CLI (`base.host.env.dotenv_boot.home_checkout_error`); a home with
    none (a test home) accepts any checkout. Only the verbs in `_READ_ONLY_VERBS`
    are exempt. Returns None to proceed, an error rc to refuse.

    `repo` is the checkout the running CLI belongs to. This gate is settings-free
    and runs before every other one, so a foreign checkout is stopped before
    anything loads the home's configuration.
    """
    from base.host.env.dotenv_boot import home_checkout_error

    path = _command_path(args_in)
    if any(path[: len(verb)] == verb for verb in _READ_ONLY_VERBS):
        return None
    error = home_checkout_error(repo)
    if error is None:
        return None
    print(f"✗ ava {' '.join(path)}: {error}", file=sys.stderr)
    return 1


def unit_already_stopped() -> bool:
    """Allow an idempotent cold stop without fetching the offline gateway."""
    from base.deploy.maintenance.pause_owner import read_for_home
    from base.host.env.dotenv_boot import resolve_ava_home

    current = read_for_home(resolve_ava_home())
    return (
        current.status == "paused"
        and current.maintenance is not None
        and current.maintenance.phase == "stopped"
        and not current.maintenance.failures
    )
