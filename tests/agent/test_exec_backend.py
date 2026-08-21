"""AVA_EXEC_BACKEND dispatch — the exec seam `_exec_with_cancel_event`
routes each execute_code run to the subprocess child (default) or the
in-process worker thread (rollback valve), and the setting itself rejects
anything outside the two values.

The pid probes distinguish the backends behaviorally: the thread backend
runs agent code inside this process, the subprocess backend inside a
disposable child.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from pydantic import ValidationError

from agent.graph._exec import _exec_with_cancel_event, _ExecDone, _ExecOutcome
from shared.config import settings
from shared.config.sandbox import SandboxSettings


def test_exec_backend_defaults_to_subprocess() -> None:
    assert settings.sandbox.exec_backend == "subprocess"


def test_exec_backend_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        SandboxSettings(AVA_EXEC_BACKEND="bogus")  # type: ignore[arg-type]  # runtime Literal validation under test


async def test_thread_backend_runs_in_process() -> None:
    outcome = await _exec_with_cancel_event(
        "import os\nprint(os.getpid())",
        "424242",
        asyncio.Event(),
        backend="thread",
    )
    assert isinstance(outcome, _ExecOutcome)
    assert isinstance(outcome.result, _ExecDone)
    assert outcome.payload is None  # no child envelope in-process
    child_pid = int(outcome.result.output.strip())
    assert child_pid == os.getpid()


async def test_subprocess_backend_runs_in_child() -> None:
    outcome = await _exec_with_cancel_event(
        "import os\nprint(os.getpid())",
        "424242",
        asyncio.Event(),
        backend="subprocess",
    )
    assert isinstance(outcome, _ExecOutcome)
    assert isinstance(outcome.result, _ExecDone)
    assert outcome.payload is not None  # the child wrote its result envelope
    assert outcome.payload.kind == "done"
    child_pid = int(outcome.result.output.strip())
    assert child_pid != os.getpid()


async def test_unknown_backend_raises() -> None:
    with pytest.raises(ValueError, match="unknown exec backend"):
        await _exec_with_cancel_event(
            "pass",
            "424242",
            asyncio.Event(),
            backend="container",  # type: ignore[arg-type] — runtime check under test
        )
