import asyncio
import os
import stat
from pathlib import Path
from threading import Event

from base.deploy.maintenance import admission
from base.events.live.bus import EventBus
from base.events.live.projection import ToUserChanged

type FileStamp = tuple[int, int, int, int, int]
type WorkspaceStamp = tuple[int, int, FileStamp | None]


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
                    _file_stamp(workspace / "to_user.md"),
                )
    return workspaces


async def watch_to_user(root: Path, bus: EventBus) -> None:

    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    stopped = Event()
    try:
        previous = await asyncio.to_thread(_snapshot, root, stopped)

        while True:
            await asyncio.sleep(0.5)
            if admission.quiesced():
                continue
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
        stopped.set()
