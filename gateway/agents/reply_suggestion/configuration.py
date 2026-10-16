from __future__ import annotations

from collections.abc import Callable
from typing import Any

from base.agents.history.timeline_inputs import TimelineReadInputs
from base.config import ConfigBoot
from base.db import Database
from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.decision.call import decide
from base.lm.effort import ReasoningEffort
from base.lm.factory import build_chat_model
from gateway.agents.history.timeline import load_current_messages
from gateway.agents.reply_suggestion.service import (
    DRAFT_TIMEOUT_S,
    ReplySuggestionPolicy,
    ReplySuggestionService,
)
from ops.agents import get_agent_status

__all__ = ["reply_suggestion_policy", "reply_suggestion_service"]


def reply_suggestion_policy(config: ConfigBoot) -> ReplySuggestionPolicy:

    return ReplySuggestionPolicy(
        enabled=lambda: config.view.lm.reply_suggestion_enabled,
        model=lambda: config.view.lm.reply_suggestion_model,
        gate_model=lambda: config.view.lm.reply_suggestion_gate_model,
        gate_threshold=lambda: config.view.lm.reply_suggestion_gate_threshold,
        max_input_tokens=lambda: config.view.lm.reply_suggestion_max_input_tokens,
    )


def reply_suggestion_service(
    config: ConfigBoot,
    *,
    db: Database,
    catalog_reader: Callable[[], ModelCatalog],
    rendering: TimelineReadInputs,
) -> ReplySuggestionService:

    policy = reply_suggestion_policy(config)

    def draft_model(model: str, catalog: ModelCatalog) -> Any:

        return build_chat_model(
            model,
            reasoning_effort=ReasoningEffort.NONE,
            timeout=DRAFT_TIMEOUT_S,
            catalog=catalog,
            llm_override=config.view.lm.llm_override,
            overrides=ModelOverrides.from_pins({}),
        )

    return ReplySuggestionService(
        policy=policy,
        catalog_reader=catalog_reader,
        rendering=rendering,
        load_messages=lambda agent_id: load_current_messages(db, agent_id),
        agent_status=lambda agent_id: get_agent_status(db, agent_id),
        decide=decide,
        draft_model=draft_model,
    )
