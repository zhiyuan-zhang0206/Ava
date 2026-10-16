from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.hooks import Hook
from agent.messages import NoteTag, system_note_message
from agent.state import AgentState, PluginStateHandle
from base.agents.context import AvaContext
from base.agents.to_user import DOCUMENT_FILENAME, read_document
from base.log import logger
from base.packages.plugins.extensions import PluginContributions
from base.packages.text_budget import count_words
from base.paths import workspace_dir

from ._state import OVER_LIMIT, AvaToUserState


def state_handle() -> PluginStateHandle[AvaToUserState]:
    return PluginStateHandle(AvaToUserState, "ava_to_user")


def _reminder_text(count: int, limit: int) -> str:
    return (
        f"Your `{DOCUMENT_FILENAME}` panel is at {count} words; its soft limit is {limit}. "
        "Nothing was blocked or rewritten — this is a nudge to keep the panel easy to scan. "
        "Links and images in the panel are shown to the user like in a normal Markdown "
        "editor (clickable, images render), so detail can live in the files you link "
        "instead of on the page. A small task can carry its whole story on the page; as it "
        "grows, keep the page as the user's entry point — current state, decisions, next "
        "steps — and move the detail into linked files."
    )


def _rearmed(recorded: AvaToUserState, compact_version: int) -> tuple[set[str], int]:
    if compact_version > recorded.last_seen_compact:
        return set(), compact_version
    return set(recorded.reminded), recorded.last_seen_compact


# ava:ref ava_builtins/plugins/ava_to_user/docs/ava_to_user.ava.okf.md#soft-limit-reminder
class _ToUserLimitHook(Hook):
    async def __call__(
        self,
        state: AgentState,
        runtime: Runtime[AvaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        handle = state_handle()
        recorded = handle.view(state)
        if not recorded.panel_written:
            return None
        reminded, bookmark = _rearmed(recorded, state.compact.version)
        cleared = handle.delta(
            {"panel_written": False, "reminded": reminded, "last_seen_compact": bookmark}
        )
        agent = runtime.context.require_agent()
        if agent.sandbox.eval_isolation:
            return cleared
        identity = runtime.context.identity
        agent_id = None if identity is None else identity.agent_id
        if agent_id is None:
            return cleared
        try:
            document = await asyncio.to_thread(read_document, workspace_dir(agent_id))
        except (OSError, ValueError):
            logger.opt(exception=True).warning(
                "to_user soft limit: the panel could not be read; no reminder"
            )
            return cleared
        # ava:ref ava_builtins/plugins/ava_to_user/docs/ava_to_user.ava.okf.md#shared-word-metric
        count = count_words(document.content or "")
        limit = agent.prompt.to_user_soft_limit_words
        if count <= limit:
            return handle.delta(
                {
                    "panel_written": False,
                    "reminded": reminded - {OVER_LIMIT},
                    "last_seen_compact": bookmark,
                }
            )
        if (
            agent.prompt.to_user_limit_reminder_cadence == "once_per_compaction"
            and OVER_LIMIT in reminded
        ):
            return cleared
        return {
            **handle.delta(
                {
                    "panel_written": False,
                    "reminded": reminded | {OVER_LIMIT},
                    "last_seen_compact": bookmark,
                }
            ),
            "messages": [
                system_note_message(
                    content=_reminder_text(count, limit),
                    tag=NoteTag.TO_USER_SOFT_LIMIT,
                    created_at=datetime.now(UTC),
                )
            ],
        }


to_user_limit_after_exec = _ToUserLimitHook()


def contribute() -> PluginContributions:
    return PluginContributions(
        after_exec=(to_user_limit_after_exec,),
        state=(AvaToUserState,),
    )
