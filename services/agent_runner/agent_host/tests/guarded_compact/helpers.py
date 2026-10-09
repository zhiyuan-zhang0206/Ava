"""Offline provider plus actual native host/graph fixtures for guarded compact faults."""

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from agent.graph._init_context import init_context_node
from agent.graph.claim.node import claim_node
from agent.impersonation import flush_checkpoint
from agent.startup import wrap_saver_writes_with_nstep_interval
from agent.state import AgentState, checkpoint_msgpack_allowlist
from base.agents.context import AvaContext
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.db import Database
from base.events.live.bus import EventBus
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.runtime import _AgentRuntime


class SummaryModel(FakeListChatModel):
    calls: int = 0
    inputs: list[str] = Field(default_factory=list)

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self

    def _call(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> str:
        self.calls += 1
        self.inputs.extend(str(message.content) for message in messages)
        return super()._call(messages, stop=stop, run_manager=run_manager, **kwargs)


async def make_host(
    aops_pool: AsyncConnectionPool,
    agent: int,
    interval: int,
    ordinary: list[object],
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed_history: bool = True,
) -> tuple[AgentHost, AsyncPostgresSaver, RunnableConfig]:
    async def model(state: AgentState, runtime: Runtime[AvaContext]) -> Command[Any]:
        work = runtime.context.native_work
        ordinary.append(None if work is None else work.work_id)
        return Command(
            update={"halted": True, "messages": [AIMessage(content="ordinary reply")]}, goto="claim"
        )

    saver = AsyncPostgresSaver(
        cast(Any, aops_pool),
        serde=JsonPlusSerializer(allowed_msgpack_modules=checkpoint_msgpack_allowlist()),
    )
    # Provisioning owns DDL; a real runner may only write existing checkpoint tables.
    wrap_saver_writes_with_nstep_interval(saver, interval)
    wrap_saver_reads_with_delta_reconstruction(saver)
    builder: Any = StateGraph(AgentState, context_schema=AvaContext)
    builder.add_node(
        "claim", claim_node, destinations=("before_llm", "init_context", "claim", "__end__")
    )
    builder.add_node("init_context", init_context_node, destinations=("claim", "__end__"))
    builder.add_node("before_llm", model, destinations=("claim",))
    builder.add_edge(START, "claim")
    graph = builder.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent)}}
    if seed_history:
        await graph.aupdate_state(
            config,
            {
                "messages": [HumanMessage(id="source-history", content="Retain this task")],
                "halted": False,
            },
        )
        await flush_checkpoint(saver, agent)
    host = AgentHost(
        pool=aops_pool,
        checkpointer=saver,
        graph=graph,
        machine="claim-test",
        bus=EventBus.from_settings(),
        db=Database.from_settings(),
    )
    monkeypatch.setattr(
        host,
        "_runtime_for",
        AsyncMock(return_value=_AgentRuntime("test", SummaryModel(responses=["unused"]))),
    )
    return host, saver, config
