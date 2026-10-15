from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from base.agents.history.timeline_inputs import TimelineReadInputs
from base.api_contracts.agent_contract import AgentStatus
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import Decision, Question
from gateway.agents.reply_suggestion.service import ReplySuggestionPolicy, ReplySuggestionService


def inbound(text: str, source: str = "user") -> HumanMessage:
    return HumanMessage(
        content=text,
        additional_kwargs={
            "ava_msg_type": "inbound",
            "ava_source": source,
            "ava_created_at": "2026-10-11T00:00:00+00:00",
        },
    )


def agent_text(text: str, message_id: str = "ai-1") -> AIMessage:
    return AIMessage(content=text, id=message_id)


def conversation(question: str = "Deploy now?") -> list[BaseMessage]:
    return [
        SystemMessage(content="system prompt"),
        inbound("Please prepare the release."),
        agent_text(question),
    ]


def useful_reply(probability: float) -> Mapping[str, Decision]:
    return {
        "useful_reply": Decision(
            top="true" if probability >= 0.5 else "false",
            probabilities={"true": probability, "false": 1 - probability},
        )
    }


class Settings:
    def __init__(self) -> None:
        self.enabled = True
        self.model = "chat-model"
        self.gate_model = "gate-model"
        self.threshold = 0.5
        self.max_input_tokens = 1500

    def policy(self) -> ReplySuggestionPolicy:
        return ReplySuggestionPolicy(
            enabled=lambda: self.enabled,
            model=lambda: self.model,
            gate_model=lambda: self.gate_model,
            gate_threshold=lambda: self.threshold,
            max_input_tokens=lambda: self.max_input_tokens,
        )


class FakeDecide:
    def __init__(self, result: Mapping[str, Decision] | None = None) -> None:
        self.result = result if result is not None else useful_reply(0.9)
        self.error: Exception | None = None
        self.calls: list[tuple[str, str, Mapping[str, Question], dict[str, Any]]] = []

    async def __call__(
        self, model: str, state: str, questions: Mapping[str, Question], **kwargs: Any
    ) -> Mapping[str, Decision]:
        self.calls.append((model, state, questions, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


class FakeChat:
    def __init__(
        self,
        reply: str = '{"suggestions": ["Yes, go ahead.", "Wait, explain the plan first."]}',
    ) -> None:
        self.reply = reply
        self.error: Exception | None = None
        self.built: list[str] = []
        self.invocations: list[Sequence[BaseMessage]] = []
        self.bindings: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.raw = AIMessage(content=reply)
        self.metadata = {"model_provider": "anthropic", "stop_reason": "end_turn"}

    def factory(self, model: str) -> FakeChat:
        self.built.append(model)
        return self

    def with_structured_output(self, schema: dict[str, Any], **kwargs: Any) -> FakeChat:
        self.bindings.append((schema, kwargs))
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> dict[str, Any]:
        self.invocations.append(messages)
        if self.error is not None:
            raise self.error
        self.raw = AIMessage(content=self.reply, response_metadata=self.metadata)
        try:
            parsed = json.loads(self.reply)
        except json.JSONDecodeError as error:
            return {"raw": self.raw, "parsed": None, "parsing_error": error}
        return {"raw": self.raw, "parsed": parsed, "parsing_error": None}


class Harness:
    def __init__(self, catalog: ModelCatalog) -> None:
        self.settings = Settings()
        self.decide = FakeDecide()
        self.chat = FakeChat()
        self.status = AgentStatus.IDLING
        self.messages: list[BaseMessage] = conversation()
        self.loads: list[int] = []
        self.service = ReplySuggestionService(
            policy=self.settings.policy(),
            catalog=catalog,
            rendering=TimelineReadInputs(_no_clock, lambda: False),
            load_messages=self._load,
            agent_status=lambda _agent_id: self.status,
            decide=self.decide,
            draft_model=self.chat.factory,
        )

    def _load(self, agent_id: int) -> list[BaseMessage]:
        self.loads.append(agent_id)
        return self.messages


def _no_clock() -> Any:
    raise AssertionError("the excerpt never renders a timestamp")
