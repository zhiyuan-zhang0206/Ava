"""Refresh the retained machine's memory checkout without interrupting indexing."""

import logging
import subprocess

from base.cluster.machine import MachineIdentity
from base.native_process.os_platform import CREATE_NO_WINDOW

__all__ = ["refresh_gateway_checkout"]

_log = logging.getLogger("services.derived.memory_indexer.daemon")


def refresh_gateway_checkout(*, machine: MachineIdentity) -> None:
    """Fast-forward the gateway checkout to origin/main — hourly safety net.

    The intended path for new merged notes to reach the index is the
    arbiter's post-merge `ava memory refresh` (bundled into `ava memory
    arbiter merge`). This catches the case where that step is skipped or
    fails: fetch + ff-only merge is cheap, and when HEAD moves the fs
    observer fires events that re-embed the changed files. A failure is
    logged at ERROR and retried next cycle — a stale index can no longer
    rot silently. Keep-local mode: `pull_main` is a no-op.
    """
    from base.deploy.git.memory_repo import pull_main
    from base.paths import gateway_memory_dir

    cwd = gateway_memory_dir(roles=machine.role())
    try:
        before = subprocess.check_output(  # noqa: S603 — argv is a static literal
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            text=True,
            creationflags=CREATE_NO_WINDOW,
        ).strip()
        head = pull_main(machine=machine)
    except Exception as exc:
        _log.error(
            "[indexer] gateway checkout refresh failed: %r — the search index may be "
            "stale; run `ava memory refresh`. Will retry next cycle.",
            exc,
        )
        return
    if head != before:
        _log.info(
            "[indexer] gateway checkout fast-forwarded %s → %s (post-merge refresh "
            "was missed — new notes are now searchable)",
            before[:8],
            head[:8],
        )
