from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

from base.host.permissions_helper.grants.catalog import (
    AUTOMATION_TARGETS,
    FOLDER_ITEMS,
    Grant,
    PermissionItem,
)
from base.host.permissions_helper.grants.probe import HelperChannel, read_result, spawn_child

FOLDER_CHILD = Path(__file__).with_name("children") / "folder_touch.py"
APPLE_EVENT_CHILD = Path(__file__).with_name("children") / "apple_event.py"

APPLE_EVENT_SCRIPTS: dict[str, str] = {
    f"automation-{suffix}": f'tell application "{app}" to get version'
    for suffix, app in AUTOMATION_TARGETS
}

FOLDER_PATHS: dict[str, str] = {
    item.id: item.home_subpath for item in FOLDER_ITEMS if item.home_subpath
}


def classify_touch(text: str) -> str:
    if text.startswith("granted"):
        return "granted"
    if text.startswith("denied"):
        return "denied"
    return f"unresolved ({text})"


def classify_apple_event(text: str) -> str:
    if text.startswith("granted"):
        return "granted"
    if text.startswith("failed") and "-1743" in text:
        return "denied (-1743: user declined the Automation dialog)"
    return f"unresolved ({text})"


def child_argv(item_id: str, *, home: Path, result_path: Path) -> list[str] | None:
    if item_id in FOLDER_PATHS:
        return [
            sys.executable,
            str(FOLDER_CHILD),
            str(home / FOLDER_PATHS[item_id]),
            str(result_path),
        ]
    if item_id in APPLE_EVENT_SCRIPTS:
        return [
            sys.executable,
            str(APPLE_EVENT_CHILD),
            APPLE_EVENT_SCRIPTS[item_id],
            str(result_path),
        ]
    return None


def request_item(
    client: HelperChannel,
    item: PermissionItem,
    *,
    workdir: Path,
    run_id: str,
    env: dict[str, str],
    home: Path,
    timeout_s: float,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    result_path = workdir / f"{item.id}-{run_id}.result"
    result_path.unlink(missing_ok=True)
    argv = child_argv(item.id, home=home, result_path=result_path)
    if argv is None:
        return "unresolved (no trigger method)"
    classify = classify_touch if item.id in FOLDER_PATHS else classify_apple_event
    spawn_child(
        client,
        name=f"meiva-permissions-{run_id}-{item.id}",
        argv=argv,
        env=env,
        workdir=workdir,
        tag=f"{item.id}-{run_id}",
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        sleep(0.5)
        text = read_result(result_path)
        if text is not None:
            return classify(text)
    return "unresolved (timed out)"


def verdict_grant(verdict: str) -> Grant:
    if verdict == "granted":
        return Grant.GRANTED
    if verdict.startswith("denied"):
        return Grant.DENIED
    return Grant.UNKNOWN
