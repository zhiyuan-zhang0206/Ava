"""Small process-environment primitives for child processes.

Runtime configuration belongs in ``base.config``. These helpers are only for
process mechanics that Settings cannot represent: copying the complete live
environment into a child, or reducing it to the mechanics a daemon may keep.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def inherited_process_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy the live environment and apply explicit child-only overrides."""
    child = dict(os.environ)
    if overrides is not None:
        child.update(overrides)
    return child


# The operator's process mechanics a long-lived native daemon may keep: binary
# lookup (a bare `redis-server` resolves through the child's PATH), identity,
# temp dir, timezone and locale (`LC_*` as a prefix). The Windows names matter
# only to a throwaway Postgres there (`pg_start_env`): a Windows child needs
# `SystemRoot` to run at all, and `pg_ctl` starts the server through `COMSPEC`.
_DAEMON_ENV_NAMES = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TZ", "LANG"}
    | {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE"}
)


def daemon_process_env() -> dict[str, str]:
    """The environment of a long-lived data-plane daemon (PgBouncer, Redis, and
    the Postgres postmaster through `base.cluster.dataplane.pg_tools.pg_start_env`).

    Only the operator's process mechanics cross, never configuration or a
    credential: the boot pass may have put the gateway login, the write
    generation and its API token into this process's environment, and a daemon
    would otherwise keep them until it restarts.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if name in _DAEMON_ENV_NAMES or name.startswith("LC_")
    }
