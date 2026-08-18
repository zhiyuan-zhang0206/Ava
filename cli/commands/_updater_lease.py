"""Updater-lease CLI entry — `python -m cli.commands._updater_lease touch|clear`.

The updater's liveness lease (R1, Task #1021): the `ava-updater` session writes
`host_deploy_state.updater_lease_expires_at` when it starts and clears it when
it exits, so the stalled-updater controller and Phase B judge "is this host's
updater alive" by lease expiry instead of the updater log's mtime.

The update legs are shell chains (POSIX `&&` / cmd.exe `&&`) that cannot import
Python state; this module is the parameter-translation seam the R1 design
reserves for them — the state machine itself lives in
`shared.host_deploy_state` (invariant: state logic only in Python, shells only
translate parameters). Called by the spawn_update chains and by the in-process
`_update_agent_runner` path.

Both verbs are deliberately fail-soft in the shell chains (`|| true` on POSIX,
`|| ver>nul` on cmd.exe): the lease is an advisory liveness signal, so a write
failure must not block the update — and a restart-only chain runs on the
PRE-checkout code, which may predate this module on the first rollout that
ships it (the update chain re-touches after `uv sync` on the new code, and the
TTL covers the gap). Exit code 0 on success, 1 on any failure.
"""

from __future__ import annotations

import sys


def _main(argv: list[str]) -> int:
    from shared.host_deploy_state import clear_updater_lease, touch_updater_lease

    verb = argv[1] if len(argv) > 1 else "touch"
    if verb == "touch":
        touch_updater_lease()
        return 0
    if verb == "clear":
        clear_updater_lease()
        return 0
    print(f"_updater_lease: unknown verb {verb!r} (expected touch|clear)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
