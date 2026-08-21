"""Parent-side machinery tests for the exec subprocess
(`agent/graph/_exec_subprocess.py`) — real children, driven directly through
`_run_in_subprocess` (the exec node is not wired to it until PR2).

Each case spawns one real child (~1s for `import ava`); keep the count low.
POSIX-only cases (signals, process groups) are skipped on Windows.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage

from agent.graph._exec_result import (
    ExecChildError,
    _ExecCancelled,
    _ExecCrashed,
    _ExecDone,
    _ExecTimedOut,
)
from agent.graph._exec_stream import ExecOutputChunkPublisher
from agent.graph._exec_subprocess import _run_in_subprocess

_AGENT_ID = 424242


async def _run(
    tmp_path: Path,
    code: str,
    *,
    timeout: float = 30.0,
    cancel_after: float | None = None,
    state: dict[str, Any] | None = None,
    chunk_publisher: ExecOutputChunkPublisher | None = None,
):
    cancel_event = asyncio.Event()
    cancel_task: asyncio.Task[None] | None = None
    if cancel_after is not None:

        async def _set_cancel() -> None:
            await asyncio.sleep(cancel_after)
            cancel_event.set()

        cancel_task = asyncio.create_task(_set_cancel())
    try:
        return await _run_in_subprocess(
            code,
            _AGENT_ID,
            cancel_event,
            timeout,
            chunk_publisher,
            state=state,
            exec_dir=tmp_path / "exec",
        )
    finally:
        # A completed run may finish before the scheduled cancel fires — make
        # sure the helper task does not leak past the test.
        if cancel_task is not None:
            cancel_task.cancel()


async def test_subprocess_done(tmp_path: Path) -> None:
    result = await _run(tmp_path, "print('hello from child')")
    assert isinstance(result, _ExecDone)
    assert "hello from child" in result.output
    # envelopes cleaned up
    assert not list((tmp_path / "exec" / str(_AGENT_ID)).iterdir())


async def test_subprocess_merged_stream_preserves_order(tmp_path: Path) -> None:
    """stderr=STDOUT at spawn level keeps print/traceback interleaving — the
    same chronological merge the old in-thread capture gave."""
    result = await _run(
        tmp_path,
        "import sys\nprint('one')\nprint('two', file=sys.stderr)\nprint('three')",
    )
    assert isinstance(result, _ExecDone)
    assert result.output.index("one") < result.output.index("two") < result.output.index("three")


async def test_subprocess_output_contains_only_agent_text(tmp_path: Path) -> None:
    """Framework text must never leak into the child's output pipe — loguru's
    default stderr handler is removed before `import ava`, so a
    Settings-construction warning (AVA_TIMEZONE unset, as in CI) goes nowhere
    near the agent's exec output."""
    result = await _run(tmp_path, "print('ok')")
    assert isinstance(result, _ExecDone)
    assert result.output == "ok\n"


async def test_subprocess_crashed_carries_child_traceback(tmp_path: Path) -> None:
    result = await _run(tmp_path, "raise ValueError('boom')")
    assert isinstance(result, _ExecCrashed)
    assert isinstance(result.exc, ExecChildError)
    assert "boom" in (result.exc.exc_msg or "")
    assert "ValueError" in (result.exc.full_traceback or "")
    assert result.full_traceback is not None  # envelope text rides the sum type
    assert "ValueError: boom" in result.output  # agent-facing traceback in output


async def test_subprocess_os_exit_without_envelope_is_crash(tmp_path: Path) -> None:
    """The agent's own os._exit leaves no envelope; the parent must not round
    that up to a clean done."""
    result = await _run(tmp_path, "import os\nos._exit(5)")
    assert isinstance(result, _ExecCrashed)
    assert "without writing a result envelope" in str(result.exc)


async def test_subprocess_timeout(tmp_path: Path) -> None:
    # The deadline must not race child boot: `import ava` alone is ~2s on CI
    # (PR #256 round 4 went red with 1.0s — the group SIGTERM landed while the
    # child was still importing, so user code never ran). 10s fires comfortably
    # mid-sleep, and the parent's clock is wall-time-from-spawn by design.
    result = await _run(
        tmp_path, "import time\nprint('started', flush=True)\ntime.sleep(60)", timeout=10.0
    )
    assert isinstance(result, _ExecTimedOut)
    assert "started" in result.output  # partial output preserved


async def test_subprocess_cancel(tmp_path: Path) -> None:
    result = await _run(tmp_path, "import time\ntime.sleep(60)", cancel_after=0.4)
    assert isinstance(result, _ExecCancelled)


async def test_subprocess_streaming_chunks_published_incrementally(tmp_path: Path) -> None:
    """The 50ms poll loop publishes accumulated chunks while the child runs —
    the frontend streaming contract."""
    emitter = MagicMock()
    publisher = ExecOutputChunkPublisher(emitter, agent_id=_AGENT_ID, item_id="7.0")
    result = await _run(
        tmp_path,
        "import time\nfor i in range(5):\n    print(f'line {i}')\n    time.sleep(0.15)",
        timeout=30.0,
        chunk_publisher=publisher,
    )
    assert isinstance(result, _ExecDone)
    # Chunks arrived before the final result (≥2 publishes = live streaming).
    assert emitter.emit.call_count >= 2


async def test_subprocess_state_snapshot_reaches_child(tmp_path: Path) -> None:
    state = {"messages": [HumanMessage(content="snapshot says hi")], "halted": False}
    result = await _run(
        tmp_path,
        "import ava\nprint(ava.state.messages[0].content)",
        state=state,
    )
    assert isinstance(result, _ExecDone)
    assert "snapshot says hi" in result.output


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
async def test_subprocess_kills_process_group_on_timeout(tmp_path: Path) -> None:
    """A grandchild the agent spawned dies with the exec child — the SIGKILL
    goes to the whole process group (the guarantee the thread model lacked)."""
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, time\n"
        f"p = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    # Same boot-headroom rule as test_subprocess_timeout: the deadline must
    # fire after the child spawned the grandchild (see that test's comment).
    result = await _run(tmp_path, code, timeout=10.0)
    assert isinstance(result, _ExecTimedOut)
    gc_pid = int(pid_file.read_text(encoding="utf-8"))
    # The grandchild may take a moment to be reaped — poll briefly.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(gc_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"grandchild pid {gc_pid} survived the exec process-group kill")
