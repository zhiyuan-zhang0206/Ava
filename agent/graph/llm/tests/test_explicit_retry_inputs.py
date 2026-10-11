"""Retry policy reads belong to their configuration owner and remain live."""

from __future__ import annotations

import asyncio
import os
import random
from typing import Any
from unittest.mock import patch

import pytest
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent.graph.llm import node
from agent.graph.llm_errors import LlmLedger, LLMStreamStallTimeoutError
from agent.state import AgentState
from base.agents.context import AvaContext
from base.config import ConfigBoot
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog


async def test_two_owner_retry_inputs_are_live_and_unknown_failures_do_not_read_them(
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
) -> None:
    def no_jitter(_low: float, _high: float) -> float:
        return 0.0

    monkeypatch.setattr(random, "uniform", no_jitter)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with patch.dict(os.environ):
        first, second = ConfigBoot(), ConfigBoot()
        for owner, initial in ((first, 2.0), (second, 7.0)):
            owner.set_field("llm_retry_initial_interval_seconds", initial)
            owner.set_field("llm_retry_max_interval_seconds", 100.0)
            owner.set_field("llm_stall_retry_max_consecutive", 0)
        reads: list[tuple[str, str]] = []

        async def run(owner: ConfigBoot, name: str, exc: Exception) -> None:
            def read(domain: str, field: str) -> Any:
                if field in {
                    "llm_stall_retry_max_consecutive",
                    "llm_retry_max_interval_seconds",
                    "llm_retry_initial_interval_seconds",
                }:
                    reads.append((name, field))
                return getattr(getattr(owner.view, domain), field)

            context = AvaContext(
                agent=AgentSlices.resolve(
                    {"llm_model": "deepseek-flash", "llm_retry_max_attempts": 3},
                    default_reader=read,
                ),
                catalog=model_catalog,
            )
            attempts: list[int] = []

            async def attempt(*_args: object) -> Command[str]:
                attempts.append(len(attempts) + 1)
                if len(attempts) == 1:
                    raise exc
                return Command(goto="before_exec")

            monkeypatch.setattr(node, "llm_attempt", attempt)
            result = await node.llm_node(
                AgentState(),
                Runtime(context=context),
                {"configurable": {"thread_id": "1000"}},
                ledger=LlmLedger(),
            )
            assert result.goto == "before_exec"
            assert attempts == [1, 2]

        unknown = RuntimeError("unknown")
        with pytest.raises(RuntimeError) as failed:
            await run(first, "first", unknown)
        assert failed.value is unknown
        assert reads == [] and sleeps == []
        failure = LLMStreamStallTimeoutError("stall")
        await run(first, "first", failure)
        await run(second, "second", failure)
        first.set_field("llm_retry_initial_interval_seconds", 5.0)
        await run(first, "first", failure)
        assert sleeps == [2.0, 7.0, 5.0]
        assert reads == [
            (owner, field)
            for owner in ("first", "second", "first")
            for field in (
                "llm_stall_retry_max_consecutive",
                "llm_retry_max_interval_seconds",
                "llm_retry_initial_interval_seconds",
            )
        ]
