"""Deploy settle-hold telemetry: one parseable line when a settle hold ends early.

Task #1820 (user forensic ruling 2026-08-27) made each deploy phase self-measuring
so a breakdown never has to be reconstructed by hand. The settle hold is the one
phase that outlives the process that started it, so it reports on its own line,
printed where the hold ends (`ops.deploy_window`):

    [rollout-telemetry] {"settle": {"dur_s": 123.4, "hosts": ["wsl"]}}
"""

from __future__ import annotations

import json


def settle_ended(*, dur_s: float | None, hosts: list[str]) -> None:
    """Print the settle hold's duration as one parseable JSON line.

    The settle phase outlives the process that took the deploy lease: the hold
    ends in a different process (`ops.deploy_window`, the early convergence
    release) or on its TTL. So this is its own `[rollout-telemetry]` JSON line,
    printed the moment an early release happens.

    `dur_s` is computed server-side (`now() - settle_started_at`, C3 task #2189)
    so cross-host clock skew never distorts it; None when the hold predates the
    column (its duration is unknowable — reported as null, never guessed). A
    settle hold that lapses on its TTL prints nothing: no process executes at
    that moment, so the duration there is the TTL itself, and the deployment
    state row keeps the start time for anyone reconstructing it.
    """
    print(  # noqa: T201
        f"[rollout-telemetry] "
        f"{json.dumps({'settle': {'dur_s': dur_s, 'hosts': sorted(hosts)}}, sort_keys=True)}"
    )
