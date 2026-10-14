"""One shallow filesystem watcher for every local agent's user context."""

from pathlib import Path

from watchfiles import (
    Change,
    awatch,  # pyright: ignore[reportUnknownVariableType] — untyped third-party stop_event union
)

from base.events.live.bus import EventBus
from base.events.live.projection import ToUserChanged


def changed_agent(root: Path, path: Path) -> tuple[int | None, bool]:
    """Map a shallow watch event to an agent and whether watch roots changed."""
    if path.parent == root and path.name.isdecimal():
        return int(path.name), True
    if (
        path.name in {"to_user.html", "to_user.md"}
        and path.parent.parent == root
        and path.parent.name.isdecimal()
    ):
        return int(path.parent.name), False
    return None, False


async def watch_to_user(root: Path, bus: EventBus) -> None:
    root.mkdir(parents=True, exist_ok=True)
    while True:
        # Never recurse into repositories or node_modules inside workspaces.
        directories = [
            p for p in root.iterdir() if p.name.isdecimal() and p.is_dir() and not p.is_symlink()
        ]
        async for changes in awatch(root, *directories, recursive=False, debounce=150):
            changed_agents: set[int] = set()
            rebuild = False
            for change, raw_path in changes:
                agent_id, roots_changed = changed_agent(root, Path(raw_path))
                if roots_changed and change == Change.modified:
                    continue
                rebuild |= roots_changed
                if agent_id is not None:
                    changed_agents.add(agent_id)
            for agent_id in changed_agents:
                await bus.publish_best_effort(
                    ToUserChanged(agent_id=agent_id).model_dump_json(), context="to_user"
                )
            if rebuild:
                break
