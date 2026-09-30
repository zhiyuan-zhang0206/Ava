"""Settings-free home anchoring for lifecycle commands.

First start publishes identity through cli.start_intent before Settings loads.
Other lifecycle commands require an existing checkout or explicit home anchor.
"""

from __future__ import annotations

import sys


def require_anchored_home(verb: str) -> int | None:
    """Refuse a verb that acts on this checkout's own cluster when the checkout
    claims none. Returns None to proceed, an error rc to refuse.

    `resolve_ava_home`'s last rule resolves a checkout with no `AVA_HOME`, no
    prod-source match and no `.ava_home` pointer to a private per-process scratch
    home, flagged `anchored=False`: it boots bare so tools and hooks keep working,
    but it owns no cluster. A verb that stops, restarts or reconfigures "this
    cluster" has nothing to act on there, and the default home it might have meant
    belongs to the prod source's own `ava`. So the family refuses with the birth
    command instead of quietly operating an empty scratch.

    This validates only the anchor. First-start identity owns reservation and
    port validation; stop must remain available to finish exact cleanup after
    a failed initialization or a recorded destroy intent.
    """
    from base.host.env.dotenv_boot import resolve_ava_home

    home, anchored = resolve_ava_home()
    if anchored:
        return None
    print(
        f"✗ ava {verb}: this checkout claims no cluster (no AVA_HOME, not the prod "
        f"source, no .ava_home pointer), so it runs on a throwaway scratch home {home} "
        f"— `ava {verb}` from here has no cluster to act on. "
        "Birth this checkout's own cluster first:\n"
        "  ava start --worktree   # from this checkout\n"
        "To act on the default home (~/.ava) deliberately, run ITS `ava` (the one on "
        "PATH), not this checkout's.",
        file=sys.stderr,
    )
    return 1


def unit_already_stopped() -> bool:
    """Allow an idempotent cold stop without fetching the offline gateway."""
    from base.deploy.maintenance.pause_owner import read_for_home
    from base.host.env.dotenv_boot import resolve_ava_home

    home, anchored = resolve_ava_home()
    if not anchored:
        return False
    current = read_for_home(home)
    return (
        current.status == "paused"
        and current.maintenance is not None
        and current.maintenance.phase == "stopped"
        and not current.maintenance.failures
    )
