"""The reply-suggestion pipeline: excerpt -> decision gate -> drafted reply.

Every collaborator is injected (message loading, agent status, the decision call, the chat
model), so the policy readers are the only configuration this module sees and tests replace
the model calls with fakes. A suggestion is an enhancement: a `DecisionError` from the gate or a provider failure the
shared classifier trusts while drafting degrades to "no suggestion" with a warning; an
unknown agent, a missing draft-model key or any unclassified error still fails the request.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from base.agents.contract import AgentStatus
from base.agents.history.timeline import TimelineItem, build_timeline_items
from base.agents.history.timeline_inputs import TimelineReadInputs
from base.lm.call import extract_text
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import Decision, DecisionError, Question, YesNo
from base.lm.errors import ErrorClass, classify_error, normalize_provider_transport_error
from base.lm.usage import log_usage_from_message
from base.log import logger
from gateway.agents.reply_suggestion.excerpt import ConversationExcerpt, build_excerpt
from gateway.agents.reply_suggestion.schemas import ReplySuggestionResponse, SuggestionReason

GATE_USAGE_KIND = "reply_suggestion_gate"
DRAFT_USAGE_KIND = "reply_suggestion"

# Protective bounds on the two provider calls, not tuning knobs: a suggestion that has not
# arrived within these is not worth showing.
GATE_TIMEOUT_S = 15.0
DRAFT_TIMEOUT_S = 20.0
# A longer draft is not a "short reply" any more; it is rejected, never silently cut.
MAX_SUGGESTION_CHARS = 500
# Completed results kept for repeated requests (several tabs, a re-focused page).
CACHE_ENTRIES = 512

AWAITING = "awaiting"
GATE_QUESTION = YesNo(
    question=(
        "Is the assistant's last message waiting for the user to reply, decide or provide input?"
    ),
    true_if=(
        "The last assistant message asks the user a question, offers options to choose from, "
        "requests confirmation or missing information, or otherwise needs the user's answer "
        "before work can continue."
    ),
    false_if=(
        "The last assistant message reports completion, progress or results and needs "
        "nothing from the user, or is a closing remark."
    ),
)

# First in the request so the provider's prompt cache covers it on every call.
DRAFT_INSTRUCTION = (
    "You draft the reply a user is about to send to an AI agent. The excerpt holds the user's "
    "latest message and what the agent said last.\n"
    "Write the one reply the user is most likely to send next, in the user's own voice: an "
    "answer, a decision, a confirmation or an instruction.\n"
    "Rules:\n"
    "- Output only the reply text, with no quotes, labels or explanation.\n"
    "- Write in the language the user writes in.\n"
    "- Keep it short: one or two sentences at most.\n"
    "- If the agent offered options, pick the most plausible one.\n"
    "- Do not ask the user's questions back to the agent."
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
        if result.reason in (None, "not_awaiting_reply"):
            self._remember(key, result)
        return result

    def _excerpt(self, agent_id: int) -> ConversationExcerpt | None:
        messages = self._load_messages(agent_id)
        items: list[TimelineItem]
        items, _ = build_timeline_items(messages, [], inputs=self._rendering)
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
            awaiting = await self._awaiting_probability(agent_id, excerpt)
        except DecisionError:
            logger.opt(exception=True).warning(
                "reply suggestion gate failed for agent {agent_id}",
                event="reply_suggestion_gate_failed",
                agent_id=agent_id,
            )
            return _none("gate_failed")
        threshold = self._policy.gate_threshold()
        logger.info(
            "reply suggestion gate for agent {agent_id}: awaiting={awaiting:.3f} threshold={threshold}",
            event="reply_suggestion_gate",
            agent_id=agent_id,
            awaiting=awaiting,
            threshold=threshold,
        )
        if awaiting < threshold:
            return _none("not_awaiting_reply")
        return await self._draft(agent_id, excerpt)

    async def _awaiting_probability(self, agent_id: int, excerpt: ConversationExcerpt) -> float:
        questions: dict[str, Question] = {AWAITING: GATE_QUESTION}
        decisions = await self._decide(
            self._policy.gate_model(),
            excerpt.text,
            questions,
            catalog=self._catalog,
            usage_kind=GATE_USAGE_KIND,
            for_agent_id=agent_id,
            timeout=GATE_TIMEOUT_S,
        )
        return decisions[AWAITING].probability("true")

    async def _draft(self, agent_id: int, excerpt: ConversationExcerpt) -> ReplySuggestionResponse:
        model_id = self._policy.model()
        llm = self._draft_model(model_id)
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
            response,
            model=model_id,
            usage_kind=DRAFT_USAGE_KIND,
            latency_ms=(time.monotonic() - started) * 1000,
            for_agent_id=agent_id,
            catalog=self._catalog,
        )
        text = extract_text(response).strip()
        if not text or len(text) > MAX_SUGGESTION_CHARS:
            logger.warning(
                "reply suggestion draft for agent {agent_id} rejected: {chars} characters",
                event="reply_suggestion_draft_rejected",
                agent_id=agent_id,
                chars=len(text),
            )
            return _none("invalid_generation")
        return ReplySuggestionResponse(suggestion=text)


def _none(reason: SuggestionReason) -> ReplySuggestionResponse:
    return ReplySuggestionResponse(reason=reason)
