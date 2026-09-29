"""Running-commit bookmark — the commit the gateway was last started on.

A single file (`$AVA_HOME/running_sha`) written by `ava start` right before
services come up, and read by the update change-detection path. The update
compares this bookmark against the latest `origin/main` (after `git fetch`)
instead of against the current `HEAD` — so a manual `git pull` between start
and update cannot empty out the diff and skip the restart.

Scope note: this is a bookmark for *change detection*, not an answer to "what
is running right now". `ava start` writes it one line before a launcher that
leaves already-running sessions alone, so it advances on a start that restarted
nothing. Status surfaces therefore ask `shared.process_sha`, which each process
freezes at its own boot; this file stays as the update baseline it always was.

The bookmark is host-local (a file on disk, not in the central DB) because
each host's running commit is independent: a gateway and an agent-runner may
be on different commits during a staggered rollout, and the gateway's own
update detection should compare its own running commit, not the cluster pin.
"""

from __future__ import annotations

from shared.paths import running_sha_path


def get() -> str | None:
    """The commit this host was last started on, or None if never recorded."""
    path = running_sha_path()
    try:
        content = path.read_text().strip()
    except FileNotFoundError:
        return None
    if not content:
        return None
    return content


def set(sha: str) -> None:
    """Record `sha` as the commit this host is now running."""
    path = running_sha_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha.strip() + "\n")


def clear() -> None:
    """Remove the bookmark (recovery — forces fallback-to-HEAD on the next
    update check)."""
    running_sha_path().unlink(missing_ok=True)
