"""Where a watcher's generated script, bootstrap and launcher live, and how stale ones go.

Everything here is a function of explicit arguments (the cluster home, the temp root, the agent
id, the live session ids), so a test passes a `tmp_path` and a set of ids.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Collection
from pathlib import Path

__all__ = ["prune_stale", "watchers_dir"]

logger = logging.getLogger(__name__)


def watchers_dir(home: Path, tmp_root: Path, agent_id: int) -> Path:
    # Generated watcher scripts live under the system temp dir, scoped per
    # cluster + agent — NOT in $MEIVA_HOME (the old global `watchers/` dir there
    # accumulated 180+ files and let co-agents overwrite each other's scripts,
    # 2026-08-02) and NOT in the workspace. A watcher reads its script +
    # bootstrap exactly once, at launch (runpy), so the files are ephemeral
    # carriers: temp storage is their natural home, and the OS reclaims
    # whatever a killed watcher leaves behind.
    #
    # The cluster segment matters: session ids are per-agent DB counters, so
    # two co-located clusters (each its own Postgres) allocate the same ids —
    # a tmp path keyed only on agent id would collide across clusters. The
    # cluster's identity IS its home path (AGENTS.md), so the home basename +
    # a short hash of the full path makes the segment unique per cluster.
    #
    # Not a durable index — the session is the source of truth; the file only
    # needs to outlive launch, and the bootstrap self-deletes both files when
    # the watcher exits (see _build_boot), so the dir stays empty except while
    # a watcher is actually running; stale pairs are pruned at the next launch.
    slug = home.name.lstrip(".") or "cluster"
    # sha1 is fine here: the digest is a directory-name uniquifier, not a
    # security boundary (S324).
    digest = hashlib.sha1(str(home).encode()).hexdigest()[:8]  # noqa: S324
    p = tmp_root / "meiva" / f"{slug}-{digest}" / str(agent_id) / "watchers"
    p.mkdir(parents=True, exist_ok=True)
    return p


_SCRIPT_FILE_RE = re.compile(r"^watcher_(\d+)\.py$")
_BOOT_FILE_RE = re.compile(r"^watcher_(\d+)_boot\.py$")
_LAUNCH_FILE_RE = re.compile(r"^watcher_(\d+)_launch\.sh$")


def prune_stale(keep: Path, alive: Collection[int] | None) -> None:
    """Delete generated watcher files whose watcher SESSION no longer exists,
    except the pair about to be written.

    A watcher reads its script + bootstrap exactly once, at launch — but
    launch is asynchronous: `spawn.run` sends the command into a fresh session
    whose shell takes a moment to come up, so the files must stay on disk
    until the child has actually started. Deleting every old pair at every
    launch (the previous behavior) could delete a sibling's not-yet-read
    files when watchers are created back-to-back, making that sibling's
    python start fail with "can't open file ... _boot.py" (observed
    2026-08-08/09: watcher_0_boot.py / watcher_3_boot.py — Bug A, task
    #1116). So prune only what is provably dead: a pair whose session id is
    not in the live session list. A running watcher's pair stays on disk (a
    few KB) and is pruned once its session closes; a hard-killed watcher's
    pair is pruned once its session is gone too. If the session list is
    unavailable, delete nothing — a stale file is harmless, a not-yet-read
    one is fatal. Files that do not match the generated-name patterns are
    left alone.
    """
    if alive is None:
        logger.warning(
            "[watcher] listing live sessions failed; skipping the prune of stale watcher files"
        )
        return  # conservative: no session info, no pruning
    d = keep.parent
    for pat in (_SCRIPT_FILE_RE, _BOOT_FILE_RE, _LAUNCH_FILE_RE):
        for f in d.iterdir():
            if f == keep or not pat.match(f.name):
                continue
            m = pat.match(f.name)
            if m and int(m.group(1)) in alive:
                continue  # that watcher's session still exists — possibly still launching
            f.unlink(missing_ok=True)
