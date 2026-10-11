"""Record which memory notes an exec call wrote — the SDK-surface half of the write check.

Runs inside the exec subprocess, as the body of the `files.write` / `append` / `edit` wraps declared in
`plugin.py`. It must stay light and harmless: no network request, no httpx or decision-model import,
and nothing it does may change what the wrapped call returns or raises. Checking the notes happens
afterwards, in the agent process (`write_checks.py`, driven by the after_exec hook).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import ava
import ava.files as _ava_files
import base.paths
from ava.sdk_surface.agent_identity import require_agent_id
from ava.sdk_surface.validation import coerce_str
from base.log import logger

from . import sdk


def _absolute(path: str | Path) -> Path:
    """The absolute path a `files.*` call on `path` resolves to.

    The memory wraps sit outside the cwd-tracking wraps of `ava_code` (plugins stack in name
    order), so they see the caller's raw path: a relative one resolves against the tracked
    `ava.cwd` when that plugin is on, else against the workspace like the core."""
    target = Path(coerce_str(path, "path", allow_types=(os.PathLike,))).expanduser()
    if target.is_absolute():
        return target
    cwd = getattr(ava, "cwd", None)
    return cwd.get() / target if cwd is not None else _ava_files.resolve(target)


def note_location(path: str | Path) -> tuple[str, str] | None:
    """`(scope, relative posix path)` when `path` is a .md file in the shared pool or in the
    agent's own memory folder, else None. `scope` is "shared" or "personal". Files in hidden
    directories are not notes."""
    target = _absolute(path).resolve()
    if target.suffix != ".md":
        return None
    roots = (
        ("shared", sdk._pool_root()),
        ("personal", base.paths.workspace_dir(require_agent_id()) / "memory"),
    )
    for scope, root in roots:
        try:
            relative = target.relative_to(root.resolve())
        except ValueError:
            continue
        if any(part.startswith(".") for part in relative.parts):
            return None
        return scope, relative.as_posix()
    return None


def _state_handle() -> Any:
    """The plugin state handle of the live exec slot, materializing a lazy slot first."""
    materialize = getattr(ava.state, "materialize", None)
    if materialize is not None:
        materialize()
    from .agent_runtime import state_handle

    return state_handle()


def record_written_note(path: str | Path) -> None:
    """Add `path` to the record of written notes when it is a memory note; no-op otherwise.

    Never raises: the write it follows has already happened and must not be disturbed, so a
    failure here is logged and the note simply goes unchecked."""
    try:
        if not ava.in_exec_turn():
            return
        located = note_location(path)
        if located is None:
            return
        scope, relative = located
        _state_handle().update({f"{scope}_notes": {relative}})
    except Exception:
        logger.opt(exception=True).warning(
            "memory write check: could not record the write to {}; the note goes unchecked", path
        )
