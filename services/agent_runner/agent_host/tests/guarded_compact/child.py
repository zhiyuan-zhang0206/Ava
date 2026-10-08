"""Offline actual hosted source/generation stopped by SIGKILL at a durable boundary."""

import asyncio
import json
import os
import sys
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.agents.compaction.commands import observe
from base.lm.plugin_providers import model_catalog, use_catalog
from services.agent_runner.agent_host.invocation.compact import apply as compact_apply
from services.agent_runner.agent_host.invocation.compact import execute as compact_execute
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel, make_host


async def park(stage: str, model: SummaryModel) -> None:
    sys.stdout.write(json.dumps({"stage": stage, "provider_calls": model.calls}) + "\n")
    sys.stdout.flush()
    await asyncio.Event().wait()


def install(
    stage: str, model: SummaryModel, patch: pytest.MonkeyPatch, host: Any, agent: int
) -> None:
    if stage == "prepared":
        original = compact_execute.save_result

        async def save(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            await park(stage, model)
            return result

        patch.setattr(compact_execute, "save_result", save)
    elif stage == "applying":
        authorize = compact_apply.authorize

        async def permit(*args: Any, **kwargs: Any) -> bool:
            result = await authorize(*args, **kwargs)
            assert result
            await park(stage, model)
            return result

        patch.setattr(compact_apply, "authorize", permit)
    elif stage == "reset":
        invoke = host._graph.ainvoke

        async def partial_reset(*args: Any, **kwargs: Any) -> Any:
            await compact_apply.flush_checkpoint(host._checkpointer, agent)
            await park(stage, model)
            return await invoke(*args, **kwargs)

        patch.setattr(host._graph, "ainvoke", partial_reset)
    elif stage in ("applied", "short"):
        close = compact_execute.close_terminal

        async def terminal(*args: Any, **kwargs: Any) -> bool:
            await park(stage, model)
            return await close(*args, **kwargs)

        patch.setattr(compact_execute, "close_terminal", terminal)
    else:
        raise ValueError("unknown actual child compact boundary")


async def main() -> None:
    agent = int(os.environ["AVA_TEST_COMPACT_AGENT"])
    stage = os.environ["AVA_TEST_COMPACT_STAGE"]
    patch = pytest.MonkeyPatch()
    model = SummaryModel(
        responses=["short" if stage == "short" else "Original child summary. " * 100]
    )
    catalog = model_catalog()
    binding = replace(catalog.bindings["gpt-"], build_single_attempt=lambda _: model)
    with use_catalog(replace(catalog, bindings={**catalog.bindings, "gpt-": binding})):
        async with AsyncConnectionPool[psycopg.AsyncConnection](
            os.environ["AVA_DB_URL"], open=False
        ) as pool:
            host, _, _ = await make_host(pool, agent, 100, [], patch)
            await host.run_turn(agent)
            with ConnectionPool[psycopg.Connection](os.environ["AVA_DB_URL"]) as sync:
                target = observe(sync, agent)
            sys.stdout.write(target.model_dump_json() + "\n")
            sys.stdout.flush()
            await asyncio.to_thread(sys.stdin.readline)
            install(stage, model, patch, host, agent)
            await host.run_turn(agent)


if __name__ == "__main__":
    asyncio.run(main())
