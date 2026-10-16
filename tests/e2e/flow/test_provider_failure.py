import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import psycopg
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from playwright.sync_api import Page, expect
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from agent.ownership.tests.ownership_support import admit_owner, agent_row
from base.agents.incarnation.native_work import activate_work, prepare_work
from base.agents.incarnation.native_work_models import NativeWorkTarget
from base.agents.incarnation.resources import ResourceBirth
from base.config import settings
from base.db import create_agent
from base.db.code_version_gate import ProcessDbGate
from base.db.transaction import async_write_transaction
from base.packages.tests.lm.providers.provider_plugin_support import (
    provider_plugin as provider_plugin,
)
from base.paths import plugins_dir
from tests.e2e._ports import FRONTEND_URL, GATEWAY_URL


@pytest.fixture
def scenario_env(truncated_db: None, provider_plugin: Callable[..., None]) -> None:
    provider_plugin(dir_name="z_broken_provider")
    (plugins_dir() / "z_broken_provider" / "provider.py").write_text(
        'raise RuntimeError("private provider startup fault")\n'
    )


async def seed_existing_work(gate: ProcessDbGate) -> tuple[NativeWorkTarget, int]:
    with psycopg.connect(settings.data_plane.db_url) as conn:
        async with AsyncConnectionPool[psycopg.AsyncConnection](
            settings.data_plane.db_url,
            min_size=1,
            max_size=2,
            kwargs={"autocommit": True, "prepare_threshold": None},
            open=False,
        ) as pool:
            agent_id = agent_row(conn)
            conn.execute(
                "UPDATE agents_meta SET incarnation_resources=%s WHERE id=%s",
                (Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")), agent_id),
            )
            conn.commit()
            incarnation = await admit_owner(pool, agent_id, database_gate=gate)
            target = NativeWorkTarget(
                work_id=uuid4(),
                agent_id=agent_id,
                machine="claim-test",
                generation=incarnation.generation,
                owner=incarnation.owner,
                protocol=1,
            )
            async with async_write_transaction(pool) as tx:
                assert await prepare_work(tx, target) == target
                await activate_work(tx, target)
        conn.execute("UPDATE agents_meta SET status='running' WHERE id=%s", (target.agent_id,))
        conn.execute("UPDATE agents SET label='Existing work' WHERE id=%s", (target.agent_id,))
        historical = create_agent(conn)
        conn.execute(
            "INSERT INTO agents_meta (id,status,spawner) VALUES (%s,'terminated','user')",
            (historical,),
        )
        conn.commit()
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": [AIMessage(content="Retained historical reply")]}
    checkpoint["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        saver.put(
            {"configurable": {"thread_id": str(historical), "checkpoint_ns": ""}},
            checkpoint,
            {"source": "input", "step": 1, "parents": {}},
            {"messages": "1"},
        )
    return target, historical


def test_broken_provider_preserves_browser_history_text_and_stop(
    gateway_proc: str,
    frontend_proc: None,
    playwright_page: Page,
    database_gate: ProcessDbGate,
) -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        target, historical = executor.submit(asyncio.run, seed_existing_work(database_gate)).result(
            30
        )
    unauthorized = httpx.get(f"{GATEWAY_URL}/api/agents", headers={"Authorization": ""})
    assert unauthorized.status_code == 401
    models = httpx.get(f"{GATEWAY_URL}/api/models")
    assert models.status_code == 503 and models.json()["code"] == "model_catalog_unavailable"
    roster = httpx.get(f"{GATEWAY_URL}/api/agents/roster").json()
    assert roster["agents"][0]["supports_vision"] is None
    page = playwright_page
    page.goto(f"{FRONTEND_URL}?agent_id={historical}")
    expect(page.get_by_text("Retained historical reply", exact=True)).to_be_visible(timeout=30_000)
    expect(
        page.get_by_text(
            "Model capabilities are unavailable. You can still send text or request a stop."
        )
    ).to_be_visible()
    page.goto(f"{FRONTEND_URL}?agent_id={target.agent_id}")
    expect(page.get_by_test_id("composer-input")).to_be_enabled(timeout=30_000)
    expect(page.get_by_role("button", name="Stop current turn")).to_be_enabled()
    page.get_by_test_id("composer-input").fill("durable text while the catalog is unavailable")
    with page.expect_response(
        lambda response: (
            response.url.endswith(f"/api/agents/{target.agent_id}/messages")
            and response.request.method == "POST"
        )
    ) as sent:
        page.get_by_role("button", name="Send message").click()
    assert sent.value.status == 201
    expect(page.get_by_test_id("composer-input")).to_have_value("")
    with page.expect_response(
        lambda response: response.url.endswith(
            f"/api/keyed/v1/agents/{target.agent_id}/cancel-work"
        )
    ) as stopped:
        page.get_by_role("button", name="Stop current turn").click()
    assert stopped.value.status == 200
    with psycopg.connect(settings.data_plane.db_url) as conn:
        assert conn.execute(
            "SELECT status FROM inbound_messages WHERE agent_id=%s AND content=%s",
            (target.agent_id, "durable text while the catalog is unavailable"),
        ).fetchone() == ("pending",)
        assert conn.execute(
            "SELECT outcome FROM native_cancel_commands WHERE agent_id=%s", (target.agent_id,)
        ).fetchone() == ("accepted",)
        assert conn.execute(
            "SELECT status FROM agents_meta WHERE id=%s", (target.agent_id,)
        ).fetchone() == ("running",)
    expect(page.get_by_role("button", name="Stop current turn")).to_be_enabled()
