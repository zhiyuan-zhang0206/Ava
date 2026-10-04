"""Hand the busy sessions a start-time sweep closed to a one-shot child that tells their owners.

The service holds no database: a crash it is recovering from (`ledger.sweep`)
is told to the owners by a child, ``python -m ops.pty_close_notices``, that
reads the notices staged for it, writes them and exits. The child is
profile-less, so it dials as an operator process does (the way `ava stop`
does), not as a service. It runs beside the serving loop, never ahead of it: a
database that is down, slow or refusing costs a log line and at most
`NOTICE_LIMIT_S`, never a delayed or failed start.

What the child must write is on disk before it starts: the sweep's notices are
merged beside anything an earlier start already staged
(`base.sessions.pty.paths.close_notices_path`) and the child removes the file
only once every notice of it is written. A child cut short by the time limit —
or failing on the database — therefore leaves the batch for the next start to
re-send, reported at ERROR with its count; the notice's idempotency key
(machine, agent, session, shell birth) keeps a re-sent notice from being
delivered twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Sequence

from base.host.env.dotenv_boot import LAUNCHER_PROFILE_ENV_KEY
from base.log import logger
from base.native_process import child_env
from base.sessions.pty import closure
from base.sessions.pty.paths import close_notices_path
from ops import pty_close_notices

# The most the child gets to dial and write before it is killed.
NOTICE_LIMIT_S = 30.0

# The one-shot child (a module-level seam: a test pins the time-limit behavior
# by running something else under the same limits).
CHILD_COMMAND: tuple[str, ...] = (sys.executable, "-m", "ops.pty_close_notices")


async def send(swept: closure.Outcome, *, child_command: Sequence[str] = CHILD_COMMAND) -> None:
    """Stage the sweep's notices beside any waiting, then drain them with the one-shot child.

    Staging runs first and on disk, so whatever the child does not write is the
    next start's to re-send; a leftover batch is reported at ERROR with its
    count, a clean run reports the batch settled. Never raises: the service
    must start whether or not the database answers.
    """
    try:
        path = close_notices_path()
        staged = pty_close_notices.stage_crash_notices(swept, path)
    except Exception as exc:  # staging is local I/O; its failure is loud, never fatal
        logger.error("pty crash notices could not be staged: {exc}", exc=exc)
        return
    if not staged:
        return
    names = sorted(notice.name for notice in staged)
    env = child_env.inherited_process_env()
    env.pop("AVA_PROCESS_PROFILE", None)
    env.pop(LAUNCHER_PROFILE_ENV_KEY, None)
    proc: asyncio.subprocess.Process | None = None
    code: int | None
    try:
        proc = await asyncio.create_subprocess_exec(*child_command, env=env)
        async with asyncio.timeout(NOTICE_LIMIT_S):
            await proc.wait()
        code = proc.returncode
    except TimeoutError:
        code = None
    except Exception as exc:  # a notice is a side channel: it never fails the service
        logger.error("pty crash notices for {names} failed: {exc}", names=names, exc=exc)
        return
    finally:
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await asyncio.shield(proc.wait())
    if code == 0:
        logger.info(
            "pty crash notices for {names}: {count} staged notice(s) settled",
            names=names,
            count=len(staged),
        )
        return
    left = len(pty_close_notices.read_pending(path))
    if code is None:
        logger.error(
            "pty crash notices for {names} timed out after {limit}s; {left} notice(s) stay "
            "staged and are re-sent on the next start",
            names=names,
            limit=NOTICE_LIMIT_S,
            left=left,
        )
    else:
        logger.error(
            "pty crash notices for {names} were not all written (exit {code}); {left} notice(s) "
            "stay staged and are re-sent on the next start",
            names=names,
            code=code,
            left=left,
        )
