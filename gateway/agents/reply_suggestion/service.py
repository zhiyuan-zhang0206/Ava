"""The reply-suggestion pipeline: excerpt -> decision gate -> drafted reply.

Every collaborator is injected (message loading, agent status, the decision call, the chat
model), so the policy readers are the only configuration this module sees and tests replace
the model calls with fakes. A suggestion is an enhancement: a `DecisionError` from the gate or a provider failure the
shared classifier trusts while drafting degrades to "no suggestion" with a warning; an
unknown agent, a missing draft-model key or any unclassified error still fails the request.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from base.agents.history.timeline import TimelineItem, build_timeline_items
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.api_contracts.agent_contract import AgentStatus
from base.lm.call import extract_text
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import Decision, DecisionError, Question, YesNo
from base.lm.compat.stop import StopCategory, classify_stop
from base.lm.errors import ErrorClass, classify_error, normalize_provider_transport_error
from base.lm.usage import log_usage_from_message
from base.log import logger
from gateway.agents.reply_suggestion.excerpt import ConversationExcerpt, build_excerpt
from gateway.agents.reply_suggestion.schemas import (
    DraftReplies,
    ReplySuggestionResponse,
    SuggestionReason,
)

GATE_USAGE_KIND = "reply_suggestion_gate"
DRAFT_USAGE_KIND = "reply_suggestion"

# Protective bounds on the two provider calls, not tuning knobs: a suggestion that has not
# arrived within these is not worth showing.
GATE_TIMEOUT_S = 15.0
DRAFT_TIMEOUT_S = 20.0
# Completed results kept for repeated requests (several tabs, a re-focused page).
CACHE_ENTRIES = 512

USEFUL_REPLY = "useful_reply"
GATE_QUESTION = YesNo(
    question="Would suggesting a reply help the user at this point in the conversation?",
    true_if="A reply suggestion would help the user respond.",
    false_if="A reply suggestion would not help the user respond.",
)

# First in the request so the provider's prompt cache covers it on every call.
DRAFT_INSTRUCTION = (
    "Draft short replies the user could send next in the user's language. "
    "Decide the appropriate number of suggestions and their content from context."
)

LoadMessages = Callable[[int], Sequence[BaseMessage]]
AgentStatusReader = Callable[[int], AgentStatus]
DecideCall = Callable[..., Awaitable[Mapping[str, Decision]]]
DraftModelFactory = Callable[[str], Any]


@dataclass(frozen=True)
class ReplySuggestionPolicy:
    """Live readers of the `reply_suggestion_*` settings; construction reads nothing."""

    enabled: Callable[[], bool]
    model: Callable[[], str]
    gate_model: Callable[[], str]
    gate_threshold: Callable[[], float]
    max_input_tokens: Callable[[], int]


def validate_models(policy: ReplySuggestionPolicy, catalog: ModelCatalog) -> None:
    """Fail at boot when the configured models are not what their settings promise.

    A chat model id in the gate setting (or the reverse) would otherwise surface only as a
    swallowed per-request warning. Skipped while the feature is off.
    """
    if not policy.enabled():
        return
    draft, gate = policy.model(), policy.gate_model()
    if draft not in catalog.models:
        raise ValueError(
            f"reply_suggestion_model {draft!r} is not a registered chat model; "
            f"available: {sorted(catalog.models)}"
        )
    if gate not in catalog.decision_models:
        raise ValueError(
            f"reply_suggestion_gate_model {gate!r} is not a registered decision model; "
            f"available: {sorted(catalog.decision_models)}"
        )


class ReplySuggestionService:
    """Answers reply-suggestion requests; holds the cache of completed results."""

    def __init__(
        self,
        *,
        policy: ReplySuggestionPolicy,
        catalog: ModelCatalog,
        rendering: TimelineReadInputs,
        load_messages: LoadMessages,
        agent_status: AgentStatusReader,
        decide: DecideCall,
        draft_model: DraftModelFactory,
    ) -> None:
        self._policy = policy
        self._catalog = catalog
        self._rendering = rendering
        self._load_messages = load_messages
        self._agent_status = agent_status
        self._decide = decide
        self._draft_model = draft_model
        self._cache: OrderedDict[tuple[int, str], ReplySuggestionResponse] = OrderedDict()

    async def suggest(self, agent_id: int) -> ReplySuggestionResponse:
        """The suggestion for `agent_id`'s latest agent message.

        Raises `AgentNotFound` for an unknown agent and the checkpoint read's own HTTP error
        when the conversation cannot be read; model failures become `gate_failed` /
        `generation_failed`.
        """
        status = await asyncio.to_thread(self._agent_status, agent_id)
        if not self._policy.enabled():
            return _none("disabled")
        if status is not AgentStatus.IDLING:
            return _none("agent_not_idle")
        excerpt = await asyncio.to_thread(self._excerpt, agent_id)
        if excerpt is None:
            return _none("no_agent_text")
        key = (agent_id, excerpt.last_message_id)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        result = await self._gate_and_draft(agent_id, excerpt)
        if result.reason in (None, "not_useful"):
            self._remember(key, result)
        return result

    def _excerpt(self, agent_id: int) -> ConversationExcerpt | None:
        messages = self._load_messages(agent_id)
        items: list[TimelineItem]
        items, _ = build_timeline_items(messages, inputs=self._rendering)
        return build_excerpt(items, self._policy.max_input_tokens())

    def _remember(self, key: tuple[int, str], result: ReplySuggestionResponse) -> None:
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_ENTRIES:
            self._cache.popitem(last=False)

    async def _gate_and_draft(
        self, agent_id: int, excerpt: ConversationExcerpt
    ) -> ReplySuggestionResponse:
        try:
            useful = await self._useful_reply_probability(agent_id, excerpt)
        except DecisionError:
            logger.opt(exception=True).warning(
                "reply suggestion gate failed for agent {agent_id}",
                event="reply_suggestion_gate_failed",
                agent_id=agent_id,
            )
            return _none("gate_failed")
        threshold = self._policy.gate_threshold()
        logger.info(
            "reply suggestion gate for agent {agent_id}: useful={useful:.3f} threshold={threshold}",
            event="reply_suggestion_gate",
            agent_id=agent_id,
            useful=useful,
            threshold=threshold,
        )
        if useful < threshold:
            return _none("not_useful")
        return await self._draft(agent_id, excerpt)

    async def _useful_reply_probability(self, agent_id: int, excerpt: ConversationExcerpt) -> float:
        questions: dict[str, Question] = {USEFUL_REPLY: GATE_QUESTION}
        decisions = await self._decide(
            self._policy.gate_model(),
            excerpt.text,
            questions,
            catalog=self._catalog,
            usage_kind=GATE_USAGE_KIND,
            for_agent_id=agent_id,
            timeout=GATE_TIMEOUT_S,
        )
        return decisions[USEFUL_REPLY].probability("true")

    async def _draft(self, agent_id: int, excerpt: ConversationExcerpt) -> ReplySuggestionResponse:
        model_id = self._policy.model()
        llm = self._draft_model(model_id).with_structured_output(
            DraftReplies.model_json_schema(),
            method="json_schema",
            strict=True,
            include_raw=True,
        )
        started = time.monotonic()
        try:
            response = await llm.ainvoke(
                [SystemMessage(content=DRAFT_INSTRUCTION), HumanMessage(content=excerpt.text)]
            )
        except Exception as exc:
            # Only a provider failure the shared classifier trusts degrades; anything it
            # cannot classify is a bug or a misconfiguration and stays loud.
            failure = normalize_provider_transport_error(exc)
            if classify_error(failure).error_class is ErrorClass.UNKNOWN:
                raise
            logger.opt(exception=True).warning(
                "reply suggestion draft failed for agent {agent_id}",
                event="reply_suggestion_draft_failed",
                agent_id=agent_id,
            )
            return _none("generation_failed")
        log_usage_from_message(
            response["raw"],
            model=model_id,
            usage_kind=DRAFT_USAGE_KIND,
            latency_ms=(time.monotonic() - started) * 1000,
            for_agent_id=agent_id,
            catalog=self._catalog,
        )
        stop, _ = classify_stop(response["raw"], stops=self._catalog.stops)
        if stop is not StopCategory.NORMAL or response["parsing_error"] is not None:
            logger.warning(
                "reply suggestion draft for agent {agent_id} rejected: incomplete or unparsable structured output",
                event="reply_suggestion_draft_rejected",
                agent_id=agent_id,
                stop=stop.value,
            )
            return _none("invalid_generation")
        try:
            # LangChain's dict parser can repair incomplete JSON. The native response
            # must itself be a complete document before accepting its parsed object.
            json.loads(extract_text(response["raw"]))
            suggestions = DraftReplies.model_validate(response["parsed"]).suggestions
        except (json.JSONDecodeError, ValidationError):
            logger.warning(
                "reply suggestion draft for agent {agent_id} rejected: invalid candidates",
                event="reply_suggestion_draft_rejected",
                agent_id=agent_id,
            )
            return _none("invalid_generation")
        return ReplySuggestionResponse(suggestions=suggestions)


def _none(reason: SuggestionReason) -> ReplySuggestionResponse:
    return ReplySuggestionResponse(suggestions=[], reason=reason)
