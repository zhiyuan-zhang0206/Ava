import asyncio
import json
import os
import sys
from uuid import uuid4

from psycopg import AsyncConnection
from psycopg.rows import TupleRow
from psycopg_pool import AsyncConnectionPool

from agent.ownership.hosted import admit_hosted_runtime
from base.agents.incarnation.native_work import activate_work
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.transaction import async_write_transaction
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from services.agent_runner.agent_host.invocation.native_work import (
    NativeWorkContinuation,
    prepare_native_invocation,
)


async def main() -> None:
    image = LoadedCommit.capture()
    gate = ProcessDbGate(version=CodeVersion(image).get, process="native-original-owner-test")
    settings.data_plane.db_url = os.environ["MEIVA_TEST_NATIVE_DB"]
    async with AsyncConnectionPool[AsyncConnection[TupleRow]](
        settings.data_plane.db_url, open=False
    ) as pool:
        agent = int(os.environ["MEIVA_TEST_NATIVE_AGENT"])
        inc = await admit_hosted_runtime(
            pool,
            agent,
            "claim-test",
            uuid4(),
            db=Database.from_settings(gate=gate),
            expected_from="running" if os.environ.get("MEIVA_TEST_NATIVE_SUCCESSOR") else "idling",
        )
        assert inc is not None
        if os.environ.get("MEIVA_TEST_NATIVE_SUCCESSOR"):
            sys.stdout.write(
                json.dumps({"generation": str(inc.generation), "owner": str(inc.owner)}) + "\n"
            )
            sys.stdout.flush()
        else:
            work = NativeWorkContinuation(uuid4())
            await prepare_native_invocation(pool, work, inc)
            assert work.target is not None
            async with async_write_transaction(pool) as conn:
                await activate_work(conn, work.target)
            sys.stdout.write(work.target.model_dump_json() + "\n")
            sys.stdout.flush()
        await asyncio.to_thread(sys.stdin.readline)


asyncio.run(main())
