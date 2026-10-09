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

import ava
from ava.sdk_surface.install import installed
from base.agents.compaction.commands import observe
from services.agent_runner.agent_host.invocation.compact import apply as compact_apply
from services.agent_runner.agent_host.invocation.compact import execute as compact_execute
from services.agent_runner.agent_host.invocation.compact import lifecycle as compact_lifecycle
from services.agent_runner.agent_host.tests.guarded_compact.helpers import SummaryModel, make_host


async def park(stage: str, model: SummaryModel) -> None:
    sys.stdout.write(json.dumps({"stage": stage, "provider_calls": model.calls}) + "\n")
    sys.stdout.flush()
    await asyncio.Event().wait()


def install(
    stage: str, model: SummaryModel, patch: pytest.MonkeyPatch, host: Any, agent: int
) -> None:
    if stage == "released":
        save_result = compact_execute.save_result

        async def before_close(*args: Any, **kwargs: Any) -> Any:
            result = await save_result(*args, **kwargs)
            sys.stdout.write(
                json.dumps({"stage": "prepared", "provider_calls": model.calls}) + "\n"
            )
            sys.stdout.flush()
            await asyncio.to_thread(sys.stdin.readline)
            return result

        async def after_close(*args: Any, **kwargs: Any) -> bool:
            await park(stage, model)
            return False

        patch.setattr(compact_execute, "save_result", before_close)
        patch.setattr(compact_lifecycle, "settle_original_restart", after_close)
        # Driver and resource tail share this owner; replace their imported seam too.
        from services.agent_runner.agent_host.invocation import driver
        from services.agent_runner.agent_host.invocation.compact import source

        patch.setattr(driver, "settle_original_restart", after_close)
        patch.setattr(source, "settle_original_restart", after_close)
    elif stage == "prepared":
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
    ava.ensure_plugins_loaded()
    installation = installed()
    assert installation is not None
    catalog = installation.require_catalog()
    binding = replace(catalog.bindings["gpt-"], build_single_attempt=lambda _: model)
    catalog = replace(catalog, bindings={**catalog.bindings, "gpt-": binding})
    patch.setattr(ava, "__plugin_installation__", replace(installation, catalog=catalog))
    async with AsyncConnectionPool[psycopg.AsyncConnection](
        os.environ["AVA_DB_URL"], open=False
    ) as pool:
        host, _, _ = await make_host(pool, agent, 100, [], patch, catalog=catalog)
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
