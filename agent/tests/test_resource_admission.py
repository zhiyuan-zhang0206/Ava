"""Actual resource admission and exact completion preserve predecessor facts."""

import asyncio
import threading
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg_pool import AsyncConnectionPool

from base.agents.incarnation.resources import (
    IncarnationResources,
    decode_resources,
)
from base.agents.incarnation.tests.test_resources import _force
from base.db import Database
from base.native_process.runtime_incarnation import RuntimeIncarnation
from tests.fixtures.pin_agent import exec_context


async def test_force_at_owner_ready_leaves_no_resurrection_blocker(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    tmp_path: Path,
    admitted_owner_ready: tuple[RuntimeIncarnation, threading.Event, threading.Event],
    database: Database,
) -> None:
    """Force before native attachment cannot freeze an unattached reservation."""
    from agent.graph.exec._result import _ExecCrashed
    from agent.graph.exec._subprocess import _run_in_subprocess
    from agent.ownership.hosted import admit_hosted_runtime
    from base.agents.incarnation.hosted_force import original_host_force

    target, ready, force_done = admitted_owner_ready
    marker = tmp_path / "must-not-run"
    failures: list[BaseException] = []
    commands: list[int] = []
    path = db_conn.execute("SHOW search_path").fetchone()
    assert path is not None
    db_conn.commit()

    def force_after_ready() -> None:
        try:
            assert ready.wait(10)
            with psycopg.connect(db_conn.info.dsn) as writer:
                writer.execute("SELECT set_config('search_path',%s,false)", (path[0],))
                writer.commit()
                with writer.transaction():
                    commands.append(_force(writer, target))
        except BaseException as exc:
            failures.append(exc)
        finally:
            force_done.set()

    thread = threading.Thread(target=force_after_ready)
    thread.start()
    result, _ = await _run_in_subprocess(
        database,
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
        exec_context(target.agent_id),
        asyncio.Event(),
        30,
        exec_dir=tmp_path,
    )
    thread.join(10)
    assert not thread.is_alive() and failures == [] and len(commands) == 1
    assert isinstance(result, _ExecCrashed)
    assert not marker.exists()
    row = db_conn.execute(
        "SELECT incarnation_resources FROM agents_meta WHERE id=%s", (target.agent_id,)
    ).fetchone()
    assert row is not None
    frozen = decode_resources(row[0])
    assert isinstance(frozen, IncarnationResources)
    assert frozen.requests == {}
    db_conn.commit()
    assert await original_host_force(
        aops_pool,
        target.agent_id,
        target.owner,
        "resource-test",
        command_id=commands[0],
        quiescent=True,
    )

    db_conn.execute(
        "UPDATE agents_meta SET status='idling',termination_source=NULL WHERE id=%s",
        (target.agent_id,),
    )
    db_conn.commit()
    successor = await admit_hosted_runtime(
        aops_pool,
        target.agent_id,
        "resource-test",
        uuid4(),
        expected_from="idling",
        db=database,
    )
    assert successor is not None and successor.generation != target.generation
