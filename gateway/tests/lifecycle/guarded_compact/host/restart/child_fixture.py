import asyncio
import json
import os
import subprocess
import sys
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from base.agents.incarnation.resources import ResourceBirth
from base.config import settings
from base.db import create_agent
from gateway.tests.test_idempotency import client as client


async def read_child(child: subprocess.Popen[str]) -> dict[str, Any]:
    assert child.stdout is not None
    line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 15)
    assert line, child.stderr.read() if child.stderr is not None else "child missing proof"
    return json.loads(line)


def start_child(dsn: str, agent: int, stage: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "gateway.tests.lifecycle.guarded_compact.host.child"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "MEIVA_DB_URL": dsn,
            "MEIVA_REDIS_URL": settings.data_plane.redis_url,
            "OPENAI_API_KEY": "test-key",
            "MEIVA_TEST_COMPACT_AGENT": str(agent),
            "MEIVA_TEST_COMPACT_STAGE": stage,
        },
    )


def prepare_agent(db_conn: psycopg.Connection) -> int:
    agent = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'idling','claim-test') "
        "ON CONFLICT(id) DO UPDATE SET status='idling',machine='claim-test'",
        (agent,),
    )
    db_conn.execute(
        "UPDATE agents_meta SET incarnation_resources=%s,config_overlay=%s WHERE id=%s",
        (
            Jsonb(ResourceBirth(birth=uuid4()).model_dump(mode="json")),
            Jsonb({"llm_model": "gpt-6.1-sol"}),
            agent,
        ),
    )
    db_conn.commit()
    return agent
