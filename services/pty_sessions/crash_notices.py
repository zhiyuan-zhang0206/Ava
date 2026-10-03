"""Hand the busy sessions a start-time sweep closed to a one-shot child that tells their owners.

The service holds no database: a crash it is recovering from (`ledger.sweep`)
is told to the owners by a child, ``python -m ops.pty_close_notices``, that
reads the sweep's closed sessions on stdin, writes one notice each over a
single pooled connection and exits. The child is profile-less, so it dials as an
operator process does (the way `ava stop` does), not as a service. It runs beside
the serving loop, never ahead of it: a database that is down, slow or refusing
costs a log line and at most `NOTICE_LIMIT_S`, never a delayed or failed start.
Nothing retries — the sweep already cleared these sessions from the ledger — and
the notice's idempotency key (machine, agent, session, shell birth) keeps a
session from being told twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys

from base.host.env.dotenv_boot import LAUNCHER_PROFILE_ENV_KEY
from base.log import logger
from base.native_process import child_env
from base.sessions.pty import closure

# The most the child gets to dial and write before it is killed.
NOTICE_LIMIT_S = 30.0


async def send(swept: closure.Outcome) -> None:
    """Run the one-shot child for `swept`'s closed sessions; log, never raise."""
    if not swept.closed:
        return
    names = sorted(closed.name for closed in swept.closed)
    env = child_env.inherited_process_env()
    env.pop("AVA_PROCESS_PROFILE", None)
    env.pop(LAUNCHER_PROFILE_ENV_KEY, None)
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "ops.pty_close_notices",
            stdin=asyncio.subprocess.PIPE,
            env=env,
        )
        async with asyncio.timeout(NOTICE_LIMIT_S):
            await proc.communicate(json.dumps(swept.to_wire()).encode())
        if proc.returncode != 0:
            logger.warning(
                "pty crash notices for {names} were not all written (exit {code})",
                names=names,
                code=proc.returncode,
            )
    except TimeoutError:
        logger.warning("pty crash notices for {names} timed out", names=names)
    except Exception as exc:  # a notice is a side channel: it never fails the service
        logger.warning("pty crash notices for {names} failed: {exc}", names=names, exc=exc)
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await asyncio.shield(proc.wait())
