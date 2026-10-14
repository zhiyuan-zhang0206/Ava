"""Shallow user-context snapshots without per-workspace native watch registration."""

import asyncio
import os
import stat
from pathlib import Path
from threading import Event

from base.events.live.bus import EventBus
from base.events.live.projection import ToUserChanged

type FileStamp = tuple[int, int, int, int, int]
type WorkspaceStamp = tuple[int, int, FileStamp | None, FileStamp | None]


def _file_stamp(path: Path) -> FileStamp | None:
    try:
        value = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _snapshot(root: Path, stopped: Event) -> dict[str, WorkspaceStamp]:
    workspaces: dict[str, WorkspaceStamp] = {}
    with os.scandir(root) as entries:
        for entry in entries:
            if stopped.is_set():
                break
            if not entry.name.isdecimal():
                continue
            try:
                value = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(value.st_mode):
                workspace = root / entry.name
                workspaces[entry.name] = (
                    value.st_dev,
                    value.st_ino,
                    _file_stamp(workspace / "to_user.html"),
                    _file_stamp(workspace / "to_user.md"),
                )
    return workspaces


async def watch_to_user(root: Path, bus: EventBus) -> None:
    """Publish coalesced changes after each shallow scan; propagate filesystem errors."""
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    stopped = Event()
    try:
        previous = await asyncio.to_thread(_snapshot, root, stopped)
        # quiesce-exempt: reads local files and publishes Redis UI invalidations; no database
        while True:
            await asyncio.sleep(0.5)
            current = await asyncio.to_thread(_snapshot, root, stopped)
            changed = {
                int(name)
                for name in previous.keys() | current.keys()
                if previous.get(name) != current.get(name)
            }
            for agent_id in sorted(changed):
                await bus.publish_best_effort(
                    ToUserChanged(agent_id=agent_id).model_dump_json(), context="to_user"
                )
            previous = current
    finally:
        # A cancelled to_thread waiter cannot cancel its running scan. Stop that
        # scan between entries instead of leaving it to traverse the remaining roots.
        stopped.set()
