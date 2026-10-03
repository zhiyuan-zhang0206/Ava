"""Actual resource admission and exact completion preserve predecessor facts."""

import asyncio
import threading
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from base.agents.incarnation.exec_owner_protocol import OwnerReady
from base.agents.incarnation.resources import (
    IncarnationResources,
    ResourceProcess,
    decode_resources,
)
from base.agents.incarnation.tests.test_resources import _admitted, _force
from base.native_process.runtime_incarnation import RuntimeIncarnation


async def test_force_at_owner_ready_leaves_no_resurrection_blocker(  # noqa: PLR0915 -- one synchronized race proof.
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force before native attachment cannot freeze an unattached reservation."""
    from agent.graph.exec import _owned_run
    from agent.graph.exec._result import _ExecCrashed
    from agent.graph.exec._subprocess import _run_in_subprocess
    from agent.ownership.hosted import admit_hosted_runtime
    from base.agents.incarnation.hosted_force import original_host_force

    target = _admitted(db_conn)
    marker = tmp_path / "must-not-run"

    def admitted(_agent_id: int) -> RuntimeIncarnation:
        return target

    monkeypatch.setattr(_owned_run, "current_incarnation", admitted)
    original_validate = _owned_run.validate_native_ready
    ready = threading.Event()
    force_done = threading.Event()
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

    def validate_then_wait(
        receipt: OwnerReady,
        launcher: ResourceProcess,
        context_path: Path,
    ) -> None:
        original_validate(receipt, launcher, context_path)
        ready.set()
        assert force_done.wait(10)

    monkeypatch.setattr(_owned_run, "validate_native_ready", validate_then_wait)
    thread = threading.Thread(target=force_after_ready)
    thread.start()
    result, _ = await _run_in_subprocess(
        f"from pathlib import Path; Path({str(marker)!r}).touch()",
        target.agent_id,
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
    )
    assert successor is not None and successor.generation != target.generation
