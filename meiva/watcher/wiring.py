"""Build the `SpawnDeps` of this process: its agent, directories, sessions and clock."""

from __future__ import annotations

import datetime
import logging
import sys
import tempfile
from pathlib import Path

from base.host.env.dotenv_boot import watcher_runner_env
from base.paths import meiva_home
from meiva.sdk_surface import agent_identity
from meiva.shell import background
from meiva.shell import sessions as sessions
from meiva.watcher import files
from meiva.watcher.spawn import SpawnDeps

__all__ = ["spawn_deps"]

logger = logging.getLogger(__name__)


def _live_session_ids() -> set[int] | None:
    """The ids of the live shell sessions, or None when the list is unavailable."""
    try:
        return set(sessions.list())
    except Exception:
        logger.warning("[watcher] listing live sessions failed", exc_info=True)
        return None


def spawn_deps(*, watchers_dir: Path | None = None) -> SpawnDeps:
    """The deps for a spawn by the bound agent; `watchers_dir` replaces the default directory."""
    agent_id = agent_identity.require_agent_id()
    return SpawnDeps(
        agent_id=agent_id,
        watchers_dir=watchers_dir
        if watchers_dir is not None
        else files.watchers_dir(meiva_home(), Path(tempfile.gettempdir()), agent_id),
        sessions=sessions,
        background=background,
        live_session_ids=_live_session_ids,
        runner_env=watcher_runner_env(),
        python=sys.executable,
        now=lambda: datetime.datetime.now(datetime.UTC),
    )
