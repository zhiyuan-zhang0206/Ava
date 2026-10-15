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
from ava.sdk_surface.installation import installed
from base.agents.compaction.commands import observe
from base.db.code_version_gate import ProcessDbGate
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import process_name
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.invocation.compact import execute as compact_execute
from services.agent_runner.agent_host.invocation.compact import lifecycle as compact_lifecycle
from services.agent_runner.agent_host.tests.guarded_compact_support import SummaryModel, make_host


async def park(stage: str, model: SummaryModel) -> None:
    sys.stdout.write(json.dumps({"stage": stage, "provider_calls": model.calls}) + "\n")
    sys.stdout.flush()
    await asyncio.Event().wait()


def install(
    stage: str, model: SummaryModel, patch: pytest.MonkeyPatch, host: AgentHost, agent: int
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
        from services.agent_runner.agent_host.invocation import driver
        from services.agent_runner.agent_host.invocation.compact import source

        patch.setattr(driver, "settle_original_restart", after_close)
        patch.setattr(source, "settle_original_restart", after_close)
    else:
        raise ValueError("unknown actual child compact boundary")


async def main(*, database_gate: ProcessDbGate) -> None:
    agent = int(os.environ["AVA_TEST_COMPACT_AGENT"])
    stage = os.environ["AVA_TEST_COMPACT_STAGE"]
    patch = pytest.MonkeyPatch()
    model = SummaryModel(responses=["Original child summary. " * 100])
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
        host, _, _ = await make_host(
            pool, agent, 100, [], patch, catalog=catalog, database_gate=database_gate
        )
        await host.run_turn(agent)
        with ConnectionPool[psycopg.Connection](os.environ["AVA_DB_URL"]) as sync:
            target = observe(sync, agent)
        sys.stdout.write(target.model_dump_json() + "\n")
        sys.stdout.flush()
        await asyncio.to_thread(sys.stdin.readline)
        install(stage, model, patch, host, agent)
        await host.run_turn(agent)


if __name__ == "__main__":
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    database_gate = ProcessDbGate(version=version.get, process=process_name(), exempt=False)
    asyncio.run(main(database_gate=database_gate))
