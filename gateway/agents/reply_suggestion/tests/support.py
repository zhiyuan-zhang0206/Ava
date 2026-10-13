"""Fakes and builders shared by the reply-suggestion tests."""

from __future__ import annotations

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
    """Mutable stand-ins for the live settings the policy reads."""

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
    """Records every call; answers with `result` or raises `error`."""

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
    """A chat model double: records the messages and answers with `reply`."""

    def __init__(self, reply: str = '["Yes, go ahead.", "Wait, explain the plan first."]') -> None:
        self.reply = reply
        self.error: Exception | None = None
        self.built: list[str] = []
        self.invocations: list[Sequence[BaseMessage]] = []

    def factory(self, model: str) -> FakeChat:
        self.built.append(model)
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.invocations.append(messages)
        if self.error is not None:
            raise self.error
        return AIMessage(content=self.reply)


class Harness:
    """A service wired to fakes, with handles to every collaborator."""

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
