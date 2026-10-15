from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

from langgraph.runtime import Runtime

from agent.hooks import Hook
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState, PluginStateHandle
from base.agents.context import AvaContext
from base.log import logger
from base.packages.plugins.extensions import PluginContributions
from base.paths import workspace_dir

from ._prompt_sections import coding_tools_section, engineering_workflow_section
from ._state import AvaCodeState


def state_handle() -> PluginStateHandle[AvaCodeState]:

    return PluginStateHandle(AvaCodeState, "ava_code")


# ava:ref ava_builtins/plugins/ava_code/docs/runtime-notes.ava.okf.md#after-exec-messages
class _InjectCwdNotesAfterExecHook(Hook):
    async def __call__(
        self,
        state: AgentState,
        _runtime: object,
        _config: object,
        /,
    ) -> dict | None:

        current = state_handle().view(state)
        notes: list = []
        update: dict = {}

        if current.cwd_note is not None:
            notes.append(
                system_note_message(
                    content=current.cwd_note,
                    tag=NoteTag.CONTEXT,
                    created_at=datetime.now(UTC),
                )
            )
            update["cwd_note"] = None

        if current.project_skills_note is not None:
            compact_v = state.compact.version
            if compact_v > current.project_skills_seen_compact:
                notes.append(
                    system_note_message(
                        content=current.project_skills_note,
                        tag=NoteTag.PROJECT_SKILLS,
                        created_at=datetime.now(UTC),
                    )
                )
                update["project_skills_seen_compact"] = compact_v

        if notes:
            logger.info("[ava_code] injecting {} finding(s) as system notes", len(notes))
            update["messages"] = notes
            return state_handle().delta(update)
        return None


def _logical_cwd_error(cwd: Path) -> OSError | None:

    try:
        mode = cwd.stat().st_mode
    except OSError as exc:
        return exc
    if not stat.S_ISDIR(mode):
        return NotADirectoryError(f"persisted ava.cwd is not a directory: {cwd}")
    return None


# ava:ref ava_builtins/plugins/ava_code/docs/runtime-notes.ava.okf.md#logical-cwd
class ValidateCwdAfterInitHook(Hook):
    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[AvaContext],
        _config: object,
        /,
    ) -> dict[str, str] | None:
        initialized = "ava_code__cwd" in state.model_fields_set
        cwd = Path(state_handle().view(state).cwd)
        exc = _logical_cwd_error(cwd) if initialized else None
        if initialized and exc is None:
            return None

        identity = runtime.context.identity
        aid = None if identity is None else identity.agent_id
        fallback = str(Path.home() if aid is None else workspace_dir(aid))
        if exc is not None:
            logger.warning(
                "[ava_code] after_init: persisted cwd {cwd!r} failed validation "
                "({exc!r}), falling back to {fallback!r} — state updated so "
                "future restarts use the new cwd",
                cwd=str(cwd),
                exc=exc,
                fallback=str(fallback),
            )
        return {"ava_code__cwd": fallback}


def contribute() -> PluginContributions:

    return PluginContributions(
        system_prompt_sections=(coding_tools_section, engineering_workflow_section),
        after_init=(ValidateCwdAfterInitHook(),),
        after_exec=(_InjectCwdNotesAfterExecHook(),),
        state=(AvaCodeState,),
    )
