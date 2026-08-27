"""Parent-side machinery tests for the exec subprocess
(`agent/graph/_exec_subprocess.py`) — real children, driven directly through
`_run_in_subprocess` (the exec node is not wired to it until PR2).

Each case spawns one real child (~1s for `import ava`); keep the count low.
POSIX-only cases (signals, process groups) are skipped on Windows.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import psutil
import psycopg
import pytest
from langchain_core.messages import HumanMessage

from agent.graph import _exec_process
from agent.graph._exec_result import (
    ExecChildError,
    _ExecCancelled,
    _ExecCrashed,
    _ExecDone,
    _ExecLifecycle,
    _ExecTimedOut,
)
from agent.graph._exec_stream import ExecOutputChunkPublisher
from agent.graph._exec_subprocess import _run_in_subprocess
from shared.config import settings
from shared.lifecycle import AgentRestart, AgentTermination, _SystemHalt
from shared.paths import logs_dir
from shared.proc import kill_process_tree
from tests._test_env_file import rewrite_line

_AGENT_ID = 424242


def _seed_agent_for_self_lifecycle() -> None:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO agents (id) VALUES (%s)", (_AGENT_ID,))


def _self_inbound_row() -> tuple[str, str, str]:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT content, kind, source FROM inbound_messages WHERE agent_id = %s ORDER BY id",
            (_AGENT_ID,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    return rows[0]


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
        # PR2 return shape: (result, raw child envelope) — the envelope feeds
        # the exec node's delta/findings extraction; tests assert on the
        # result here.
        result, _payload = await _run_in_subprocess(
            code,
            _AGENT_ID,
            cancel_event,
            timeout,
            chunk_publisher,
            state=state,
            exec_dir=tmp_path / "exec",
        )
        return result
    finally:
        # A completed run may finish before the scheduled cancel fires — make
        # sure the helper task does not leak past the test.
        if cancel_task is not None:
            cancel_task.cancel()


async def _assert_tree_gone(pids: list[int], timeout_s: float = 5.0) -> None:
    # After SIGKILL, dead descendants can remain zombies until their new parent
    # reaps them, and psutil.pid_exists() still reports those entries. Assert no live
    # members, not that every process-table entry vanished.
    deadline = time.monotonic() + timeout_s
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        for pid in list(remaining):
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    remaining.discard(pid)
            except psutil.NoSuchProcess:
                remaining.discard(pid)
        if remaining:
            await asyncio.sleep(0.05)
    assert not remaining, f"process(es) still alive after exec teardown: {sorted(remaining)}"


async def test_assert_tree_gone_accepts_missing_pid_immediately() -> None:
    await asyncio.wait_for(_assert_tree_gone([999_999_999]), timeout=0.5)


async def test_assert_tree_gone_waits_for_live_pid_to_become_zombie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = MagicMock()
    process.status.side_effect = [psutil.STATUS_SLEEPING, psutil.STATUS_ZOMBIE]
    monkeypatch.setattr(psutil, "Process", MagicMock(return_value=process))

    await _assert_tree_gone([12345], timeout_s=0.3)


async def test_assert_tree_gone_rejects_pid_still_running_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = MagicMock()
    process.status.return_value = psutil.STATUS_RUNNING
    monkeypatch.setattr(psutil, "Process", MagicMock(return_value=process))

    with pytest.raises(AssertionError, match="still alive after exec teardown"):
        await _assert_tree_gone([12345], timeout_s=0.3)


async def test_subprocess_done(tmp_path: Path) -> None:
    result = await _run(tmp_path, "print('hello from child')")
    assert isinstance(result, _ExecDone)
    assert "hello from child" in result.output
    # envelopes cleaned up
    assert not list((tmp_path / "exec" / str(_AGENT_ID)).iterdir())


async def test_exec_child_disables_otlp_after_cluster_env_authority(tmp_path: Path) -> None:
    env_path = Path(os.environ["AVA_HOME"]) / ".env"
    original_env = env_path.read_text()
    log_path = logs_dir() / f"agent-{_AGENT_ID}.log"
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    rewrite_line(env_path, "AVA_TELEMETRY_OTLP_ENABLED", "true")
    try:
        result = await _run(
            tmp_path,
            (
                "from shared.telemetry_otlp import backend\n"
                'print("OTLP_ENABLED_IN_CHILD:", backend._enabled())\n'
            ),
        )
    finally:
        env_path.write_text(original_env)

    assert isinstance(result, _ExecDone)
    assert result.output == "OTLP_ENABLED_IN_CHILD: False\n"
    child_log = log_path.read_bytes()[log_offset:] if log_path.exists() else b""
    assert b"OTLP backend init failed" not in child_log


async def test_subprocess_bootstrap_ignores_agent_package_in_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persisted coding cwd may itself be an old Ava checkout. Its
    top-level ``agent`` package must not shadow the exec-child entry, and
    spawning the disposable child must not change the parent's OS cwd."""
    old_checkout = tmp_path / "old-checkout"
    (old_checkout / "agent").mkdir(parents=True)
    (old_checkout / "agent" / "__init__.py").write_text("# old checkout\n")
    monkeypatch.chdir(old_checkout)
    monkeypatch.setenv("PYTHONPATH", str(old_checkout))

    result = await _run(
        tmp_path,
        ("import agent.exec_child as entry\nprint('exec-child', entry.__file__)\n"),
    )

    assert isinstance(result, _ExecDone)
    expected_entry = Path(__file__).resolve().parents[2] / "agent" / "exec_child.py"
    assert str(expected_entry) in result.output
    assert Path.cwd() == old_checkout.resolve()


async def test_subprocess_bootstrap_forces_utf8_when_python_env_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated mode ignores Python encoding env vars, so the production
    child command itself must force UTF-8 on every platform, including Windows."""
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")

    result = await _run(
        tmp_path,
        (
            "import sys\n"
            "print('utf8-mode', sys.flags.utf8_mode)\n"
            "print('stdout-encoding', sys.stdout.encoding)\n"
            "print('cjk', '\u5b50\u8fdb\u7a0b')\n"
        ),
    )

    assert isinstance(result, _ExecDone)
    assert "utf8-mode 1" in result.output
    assert "stdout-encoding utf-8" in result.output.lower()
    assert "cjk \u5b50\u8fdb\u7a0b" in result.output


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


async def test_teardown_failure_is_returned_as_crash_with_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_settle_resources = _exec_process.settle_resources
    teardown_failure = RuntimeError("synthetic reader teardown failure")

    async def _fail_after_settling(
        *args: Any, **kwargs: Any
    ) -> tuple[_exec_process.TeardownFailure, ...]:
        assert not await real_settle_resources(*args, **kwargs)
        return (_exec_process.TeardownFailure("reader_join", teardown_failure),)

    monkeypatch.setattr(_exec_process, "settle_resources", _fail_after_settling)

    result = await _run(tmp_path, "print('partial before teardown')")

    assert isinstance(result, _ExecCrashed)
    assert isinstance(result.exc, _exec_process.ExecTeardownError)
    assert "partial before teardown" in result.output
    assert "reader_join: RuntimeError: synthetic reader teardown failure" in result.output


@pytest.mark.parametrize("exit_code", [5, 124])
async def test_abrupt_root_exit_stops_descendant_before_reader_cleanup(
    tmp_path: Path, exit_code: int
) -> None:
    """Neither user ``os._exit`` nor the watchdog's 124 hard-exit may bypass
    the parent-owned tree barrier and leave a stdout holder behind."""
    pid_file = tmp_path / f"abrupt-{exit_code}.pid"
    code = (
        "import os, pathlib, subprocess, sys\n"
        "descendant = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        ")\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(\n"
        "    str(descendant.pid), encoding='utf-8'\n"
        ")\n"
        f"os._exit({exit_code})\n"
    )
    descendant_pid: int | None = None
    try:
        result = await _run(tmp_path, code)
        assert isinstance(result, _ExecCrashed)
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        await _assert_tree_gone([descendant_pid])
        assert not any(
            thread.name == f"exec-reader-{_AGENT_ID}" for thread in threading.enumerate()
        )
    finally:
        if descendant_pid is None and pid_file.exists():
            descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        if descendant_pid is not None:
            kill_process_tree(descendant_pid, grace_s=0.0)


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


async def test_natural_exit_reaps_ordinary_descendant_holding_stdout(tmp_path: Path) -> None:
    """Returning from agent code ends raw subprocesses in the disposable run;
    an inherited stdout fd cannot leave a process or reader behind."""
    pid_file = tmp_path / "natural-descendant.pid"
    code = (
        "import pathlib, subprocess, sys\n"
        "descendant = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        ")\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(\n"
        "    str(descendant.pid), encoding='utf-8'\n"
        ")\n"
    )
    descendant_pid: int | None = None
    try:
        result = await _run(tmp_path, code)
        assert isinstance(result, _ExecDone)
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        await _assert_tree_gone([descendant_pid])
        assert not any(
            thread.name == f"exec-reader-{_AGENT_ID}" for thread in threading.enumerate()
        )
    finally:
        if descendant_pid is not None:
            kill_process_tree(descendant_pid, grace_s=0.0)


async def test_outer_task_cancel_reaps_child_and_descendant(tmp_path: Path) -> None:
    """Graph-task cancellation is a teardown barrier: the direct child and an
    ordinary descendant are gone and reaped before CancelledError escapes."""
    pid_file = tmp_path / "exec-tree.pids"
    code = (
        "import os, pathlib, subprocess, sys, time\n"
        "descendant = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        ")\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(\n"
        "    f'{os.getpid()} {descendant.pid}', encoding='utf-8'\n"
        ")\n"
        "print('tree-ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    task = asyncio.create_task(
        _run_in_subprocess(
            code,
            _AGENT_ID,
            asyncio.Event(),
            60.0,
            exec_dir=tmp_path / "exec",
        )
    )
    pids: list[int] = []
    try:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not pid_file.exists():
            await asyncio.sleep(0.05)
        assert pid_file.exists(), "exec child never reached user code"
        pids = [int(value) for value in pid_file.read_text(encoding="utf-8").split()]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=15.0)

        await _assert_tree_gone(pids)
        assert not any(
            thread.name == f"exec-reader-{_AGENT_ID}" for thread in threading.enumerate()
        )
        assert not list((tmp_path / "exec" / str(_AGENT_ID)).iterdir())
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for pid in reversed(pids):
            kill_process_tree(pid, grace_s=0.0)
        if os.name != "nt" and pids:
            with contextlib.suppress(ChildProcessError, ProcessLookupError):
                os.waitpid(pids[0], 0)


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


async def test_subprocess_silent_child_publishes_keepalive(tmp_path: Path) -> None:
    """A silent live child still publishes an empty keepalive frame."""
    emitter = MagicMock()
    publisher = ExecOutputChunkPublisher(emitter, agent_id=_AGENT_ID, item_id="7.0")

    result = await _run(
        tmp_path,
        "import time; time.sleep(1.3)",
        timeout=30.0,
        chunk_publisher=publisher,
    )

    assert isinstance(result, _ExecDone)
    assert result.output == ""
    events = [json.loads(call.args[0]) for call in emitter.emit.call_args_list]
    keepalives = [event for event in events if event["keepalive"] is True]
    assert keepalives
    assert all(event["content"] == "" for event in keepalives)


def test_chunk_publisher_real_output_resets_keepalive_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real output is activity, so it postpones the next keepalive."""
    now = 0.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    emitter = MagicMock()
    publisher = ExecOutputChunkPublisher(emitter, agent_id=_AGENT_ID, item_id="7.0")

    now = 0.5
    publisher.maybe_keepalive()
    now = 0.75
    publisher.publish("output")
    now = 1.24
    publisher.maybe_keepalive()
    assert emitter.emit.call_count == 2

    now = 1.25
    publisher.maybe_keepalive()
    events = [json.loads(call.args[0]) for call in emitter.emit.call_args_list]
    assert [(event["content"], event["keepalive"]) for event in events] == [
        ("", True),
        ("output", False),
        ("", True),
    ]


async def test_subprocess_state_snapshot_reaches_child(tmp_path: Path) -> None:
    state = {"messages": [HumanMessage(content="snapshot says hi")], "halted": False}
    result = await _run(
        tmp_path,
        "import ava\nprint(ava.state.messages[0].content)",
        state=state,
    )
    assert isinstance(result, _ExecDone)
    assert "snapshot says hi" in result.output


async def test_subprocess_self_terminate_lifecycle_and_inbound(tmp_path: Path) -> None:
    _seed_agent_for_self_lifecycle()

    result = await _run(tmp_path, "import ava; ava.self.terminate()")

    assert isinstance(result, _ExecLifecycle)
    assert isinstance(result.exc, AgentTermination)
    _content, kind, source = _self_inbound_row()
    assert kind == "terminate"
    assert source == "self"


async def test_subprocess_self_restart_lifecycle_and_inbound(tmp_path: Path) -> None:
    _seed_agent_for_self_lifecycle()

    result = await _run(tmp_path, "import ava; ava.self.restart()")

    assert isinstance(result, _ExecLifecycle)
    assert isinstance(result.exc, AgentRestart)
    _content, kind, source = _self_inbound_row()
    assert kind == "restart"
    assert source == "self"


async def test_subprocess_self_compact_lifecycle_and_inbound(tmp_path: Path) -> None:
    _seed_agent_for_self_lifecycle()

    result = await _run(tmp_path, "import ava; ava.self.compact('audit e2e summary')")

    assert isinstance(result, _ExecLifecycle)
    assert isinstance(result.exc, _SystemHalt)
    content, kind, _source = _self_inbound_row()
    assert kind == "compact_summary"
    assert content == "audit e2e summary"


async def test_subprocess_unknown_lifecycle_class_crashes(tmp_path: Path) -> None:
    result = await _run(
        tmp_path,
        (
            "from shared.lifecycle import _LifecycleExit\n"
            "class _MysteryLifecycle(_LifecycleExit):\n"
            "    def __init__(self):\n"
            "        super().__init__(0)\n"
            "raise _MysteryLifecycle()\n"
        ),
    )

    assert isinstance(result, _ExecCrashed)
    assert isinstance(result.exc, ExecChildError)
    assert result.exc.exc_type == "unknown_lifecycle_class"
    assert result.exc.exc_msg == ("child reported unknown lifecycle class '_MysteryLifecycle'")


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
