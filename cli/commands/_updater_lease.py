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

`--handoff-generation` CAS-claims the exact pending host-local handoff as
``running`` before the native chain mutates checkout or services. The recorded
owner is the root ``cmd.exe`` parent, which synchronously spans the complete
chain. A DB touch remains fail-soft; the handoff stays until an exact-generation
terminal clear. Exit code 0 on success, 1 on any failure.
"""

from __future__ import annotations

import sys


def _main(argv: list[str]) -> int:
    import argparse

    from shared.host_deploy_state import clear_updater_lease, touch_updater_lease

    parser = argparse.ArgumentParser(prog="python -m cli.commands._updater_lease")
    parser.add_argument("verb", choices=("touch", "clear"), nargs="?", default="touch")
    parser.add_argument("--handoff-generation", default=None)
    args = parser.parse_args(argv[1:])
    if args.verb == "touch":
        if args.handoff_generation is not None:
            import os

            from shared import ui_update_state, updater_handoff
            from shared.cluster import session_name

            with ui_update_state.lifecycle_lock():
                claimed = updater_handoff.claim_running(
                    args.handoff_generation,
                    expected_session=session_name("updater"),
                    owner_pid=os.getppid(),
                )
            if not claimed:
                return 1
        try:
            touch_updater_lease()
        except Exception:
            if args.handoff_generation is None:
                return 1
            # Observability is fail-soft. The running handoff's exact native
            # process identity remains recovery's proof even without Postgres.
            return 0
        return 0
    ok = True
    try:
        clear_updater_lease()
    except Exception:
        ok = False
    if args.handoff_generation is not None:
        from shared import updater_handoff

        try:
            ok = updater_handoff.clear(args.handoff_generation) and ok
        except Exception:
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
