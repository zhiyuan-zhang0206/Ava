from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import ava
import ava.files as _ava_files
import base.paths
from ava.sdk_surface import process_context
from ava.sdk_surface.agent_identity import require_agent_id
from ava.sdk_surface.validation import coerce_str
from base.agents.to_user.models import DOCUMENT_FILENAME
from base.log import logger


def _absolute(path: str | Path) -> Path:
    target = Path(coerce_str(path, "path", allow_types=(os.PathLike,))).expanduser()
    if target.is_absolute():
        return target
    cwd = getattr(ava, "cwd", None)
    return cwd.get() / target if cwd is not None else _ava_files.resolve(target)


# ava:ref ava_builtins/plugins/ava_to_user/docs/ava_to_user.ava.okf.md#panel-write-observation
def panel_path(path: str | Path, *, agent_id: int) -> Path | None:
    target = _absolute(path).resolve()
    if target.name != DOCUMENT_FILENAME:
        return None
    if target != (base.paths.workspace_dir(agent_id) / DOCUMENT_FILENAME).resolve():
        return None
    return target


def _state_handle() -> Any:
    materialize = getattr(ava.state, "materialize", None)
    if materialize is not None:
        materialize()
    from .agent_runtime import state_handle

    return state_handle()


def record_panel_write(path: str | Path) -> None:
    try:
        if not process_context.in_exec_turn():
            return
        if panel_path(path, agent_id=require_agent_id()) is None:
            return
        _state_handle().update({"panel_written": True})
    except Exception:
        logger.opt(exception=True).warning(
            "to_user soft limit: could not record the write to {}; the panel goes unchecked",
            path,
        )
