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

from agent.graph.exec._result import (
    ExecCancelled,
    ExecChildError,
    ExecCrashed,
    ExecDone,
    ExecLifecycle,
    ExecTimedOut,
)
from agent.graph.exec._stream import ExecOutputChunkPublisher
from agent.graph.exec._subprocess import run_in_subprocess
from ava.tests.fixtures.pin_agent import exec_context
from base.agents.lifecycle import AgentRestart, AgentTermination, SystemHalt
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.env.dotenv_file import upsert_env
from base.host.proc import kill_process_tree
from base.paths import logs_dir

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
    database_gate: ProcessDbGate,
):
    cancel_event = asyncio.Event()
    cancel_task: asyncio.Task[None] | None = None
    if cancel_after is not None:

        async def _set_cancel() -> None:
            await asyncio.sleep(cancel_after)
            cancel_event.set()

        cancel_task = asyncio.create_task(_set_cancel())
    try:
        result, _payload = await run_in_subprocess(
            Database.from_settings(gate=database_gate),
            code,
            exec_context(_AGENT_ID),
            cancel_event,
            timeout,
            chunk_publisher,
            state=state,
            exec_dir=tmp_path / "exec",
            accumulation_max_chars=1_000_000,
        )
        return result
    finally:
        if cancel_task is not None:
            cancel_task.cancel()


async def _assert_tree_gone(pids: list[int], timeout_s: float = 5.0) -> None:
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


async def test_subprocess_done(tmp_path: Path, database_gate: ProcessDbGate) -> None:
    result = await _run(tmp_path, "print('hello from child')", database_gate=database_gate)
    assert isinstance(result, ExecDone)
    assert "hello from child" in result.output
    assert not list((tmp_path / "exec" / str(_AGENT_ID)).iterdir())


async def test_exec_child_disables_otlp_after_cluster_env_authority(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    env_path = Path(os.environ["AVA_HOME"]) / ".env"
    original_env = env_path.read_text()
    log_path = logs_dir() / f"agent-{_AGENT_ID}.log"
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    upsert_env(env_path, {"AVA_TELEMETRY_OTLP_ENABLED": "true"})
    try:
        result = await _run(
            tmp_path,
            (
                "import ava\n"
                "from base.telemetry.otlp import telemetry_otlp\n"
                "probes = []\n"
                "def endpoint_probe(endpoint):\n"
                "    probes.append(endpoint)\n"
                "    return False\n"
                "telemetry_otlp.endpoint_reachable = endpoint_probe\n"
                "ava.context.clients.event_pipeline().warmup_exports()\n"
                'print("OTLP_ENABLED_IN_CHILD:", bool(probes))\n'
            ),
            database_gate=database_gate,
        )
    finally:
        env_path.write_text(original_env)

    assert isinstance(result, ExecDone)
    assert result.output == "OTLP_ENABLED_IN_CHILD: False\n"
    child_log = log_path.read_bytes()[log_offset:] if log_path.exists() else b""
    assert b"OTLP backend init failed" not in child_log


async def test_subprocess_bootstrap_ignores_agent_package_in_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
    old_checkout = tmp_path / "old-checkout"
    (old_checkout / "agent").mkdir(parents=True)
    (old_checkout / "agent" / "__init__.py").write_text("# old checkout\n")
    monkeypatch.chdir(old_checkout)
    monkeypatch.setenv("PYTHONPATH", str(old_checkout))

    result = await _run(
        tmp_path,
        ("import agent.execution.child as entry\nprint('exec-child', entry.__file__)\n"),
        database_gate=database_gate,
    )

    assert isinstance(result, ExecDone)
    expected_entry = Path(__file__).resolve().parents[2] / "agent" / "execution" / "child.py"
    assert str(expected_entry) in result.output
    assert Path.cwd() == old_checkout.resolve()


async def test_subprocess_bootstrap_forces_utf8_when_python_env_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_gate: ProcessDbGate
) -> None:
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
        database_gate=database_gate,
    )

    assert isinstance(result, ExecDone)
    assert "utf8-mode 1" in result.output
    assert "stdout-encoding utf-8" in result.output.lower()
    assert "cjk \u5b50\u8fdb\u7a0b" in result.output


async def test_subprocess_merged_stream_preserves_order(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    result = await _run(
        tmp_path,
        "import sys\nprint('one')\nprint('two', file=sys.stderr)\nprint('three')",
        database_gate=database_gate,
    )
    assert isinstance(result, ExecDone)
    assert result.output.index("one") < result.output.index("two") < result.output.index("three")


async def test_subprocess_output_contains_only_agent_text(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    result = await _run(tmp_path, "print('ok')", database_gate=database_gate)
    assert isinstance(result, ExecDone)
    assert result.output == "ok\n"


async def test_subprocess_crashed_carries_child_traceback(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    result = await _run(tmp_path, "raise ValueError('boom')", database_gate=database_gate)
    assert isinstance(result, ExecCrashed)
    assert isinstance(result.exc, ExecChildError)
    assert "boom" in (result.exc.exc_msg or "")
    assert "ValueError" in (result.exc.full_traceback or "")
    assert result.full_traceback is not None
    assert "ValueError: boom" in result.output


async def test_subprocess_os_exit_without_envelope_is_crash(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    result = await _run(tmp_path, "import os\nos._exit(5)", database_gate=database_gate)
    assert isinstance(result, ExecCrashed)
    assert "without writing a result envelope" in str(result.exc)


@pytest.mark.parametrize("exit_code", [5, 124])
async def test_abrupt_root_exit_stops_descendant_before_reader_cleanup(
    tmp_path: Path, exit_code: int, database_gate: ProcessDbGate
) -> None:
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
        result = await _run(tmp_path, code, database_gate=database_gate)
        assert isinstance(result, ExecCrashed)
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


@pytest.mark.parametrize(
    ("fault", "trigger"),
    [
        ("sigkill", "os.kill(os.getpid(), signal.SIGKILL)"),
        ("segfault", "ctypes.string_at(0)"),
    ],
)
async def test_signal_killed_child_is_a_crash_and_the_host_keeps_serving(
    tmp_path: Path, fault: str, trigger: str, database_gate: ProcessDbGate
) -> None:
    pid_file = tmp_path / f"{fault}.pid"
    code = (
        "import ctypes, os, pathlib, signal, subprocess, sys\n"
        "descendant = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        ")\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(descendant.pid), encoding='utf-8')\n"
        "print('before the fault', flush=True)\n"
        f"{trigger}\n"
    )
    descendant_pid: int | None = None
    try:
        result = await _run(tmp_path, code, database_gate=database_gate)
        assert isinstance(result, ExecCrashed)
        assert "without writing a result envelope" in str(result.exc)
        assert "before the fault" in result.output
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        await _assert_tree_gone([descendant_pid])
        assert not any(
            thread.name == f"exec-reader-{_AGENT_ID}" for thread in threading.enumerate()
        )

        after = await _run(tmp_path, "print('still serving')", database_gate=database_gate)
        assert isinstance(after, ExecDone)
        assert after.output == "still serving\n"
    finally:
        if descendant_pid is None and pid_file.exists():
            descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        if descendant_pid is not None:
            kill_process_tree(descendant_pid, grace_s=0.0)


async def test_subprocess_timeout(tmp_path: Path, database_gate: ProcessDbGate) -> None:
    result = await _run(
        tmp_path,
        "import time\nprint('started', flush=True)\ntime.sleep(60)",
        timeout=10.0,
        database_gate=database_gate,
    )
    assert isinstance(result, ExecTimedOut)
    assert "started" in result.output


async def test_subprocess_cancel(tmp_path: Path, database_gate: ProcessDbGate) -> None:
    result = await _run(
        tmp_path, "import time\ntime.sleep(60)", cancel_after=0.4, database_gate=database_gate
    )
    assert isinstance(result, ExecCancelled)


async def test_natural_exit_reaps_ordinary_descendant_holding_stdout(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
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
        result = await _run(tmp_path, code, database_gate=database_gate)
        assert isinstance(result, ExecDone)
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        await _assert_tree_gone([descendant_pid])
        assert not any(
            thread.name == f"exec-reader-{_AGENT_ID}" for thread in threading.enumerate()
        )
    finally:
        if descendant_pid is not None:
            kill_process_tree(descendant_pid, grace_s=0.0)


async def test_outer_task_cancel_reaps_child_and_descendant(
    tmp_path: Path, database: Database
) -> None:
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
        run_in_subprocess(
            database,
            code,
            exec_context(_AGENT_ID),
            asyncio.Event(),
            60.0,
            exec_dir=tmp_path / "exec",
            accumulation_max_chars=1_000_000,
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
        if pids:
            with contextlib.suppress(ChildProcessError, ProcessLookupError):
                os.waitpid(pids[0], 0)


async def test_subprocess_streaming_chunks_published_incrementally(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    emitter = MagicMock()
    publisher = ExecOutputChunkPublisher(emitter, agent_id=_AGENT_ID, item_id="7.0")
    result = await _run(
        tmp_path,
        "import time\nfor i in range(5):\n    print(f'line {i}')\n    time.sleep(0.15)",
        timeout=30.0,
        chunk_publisher=publisher,
        database_gate=database_gate,
    )
    assert isinstance(result, ExecDone)
    assert emitter.emit.call_count >= 2


async def test_subprocess_silent_child_publishes_keepalive(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    emitter = MagicMock()
    publisher = ExecOutputChunkPublisher(emitter, agent_id=_AGENT_ID, item_id="7.0")

    result = await _run(
        tmp_path,
        "import time; time.sleep(1.3)",
        timeout=30.0,
        chunk_publisher=publisher,
        database_gate=database_gate,
    )

    assert isinstance(result, ExecDone)
    assert result.output == ""
    events = [json.loads(call.args[0]) for call in emitter.emit.call_args_list]
    keepalives = [event for event in events if event["keepalive"] is True]
    assert keepalives
    assert all(event["content"] == "" for event in keepalives)


def test_chunk_publisher_real_output_resets_keepalive_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


async def test_subprocess_state_snapshot_reaches_child(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    state = {"messages": [HumanMessage(content="snapshot says hi")], "halted": False}
    result = await _run(
        tmp_path,
        "import ava\nprint(ava.state.messages[0].content)",
        state=state,
        database_gate=database_gate,
    )
    assert isinstance(result, ExecDone)
    assert "snapshot says hi" in result.output


def _self_lifecycle_code(action: str) -> str:
    return (
        "import ava, os, psycopg\n"
        "from base.host.env.registry import ADMIN_DATA_PLANE_ALIASES\n"
        "assert not ADMIN_DATA_PLANE_ALIASES.intersection(os.environ)\n"
        "with psycopg.connect(ava.DB_URL) as conn:\n"
        "    assert conn.execute('SELECT current_user').fetchone() == ('ava_g0_runner',)\n"
        f"{action}\n"
    )


@pytest.mark.usefixtures("runner_exec_env")
async def test_subprocess_self_terminate_lifecycle_and_inbound(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    _seed_agent_for_self_lifecycle()

    result = await _run(
        tmp_path, _self_lifecycle_code("ava.self.terminate()"), database_gate=database_gate
    )

    assert isinstance(result, ExecLifecycle)
    assert isinstance(result.exc, AgentTermination)
    _content, kind, source = _self_inbound_row()
    assert kind == "terminate"
    assert source == "self"


@pytest.mark.usefixtures("runner_exec_env")
async def test_subprocess_self_restart_lifecycle_and_inbound(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    _seed_agent_for_self_lifecycle()

    result = await _run(
        tmp_path, _self_lifecycle_code("ava.self.restart()"), database_gate=database_gate
    )

    assert isinstance(result, ExecLifecycle)
    assert isinstance(result.exc, AgentRestart)
    _content, kind, source = _self_inbound_row()
    assert kind == "restart"
    assert source == "self"


@pytest.mark.usefixtures("runner_exec_env")
async def test_subprocess_self_compact_lifecycle_and_inbound(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    _seed_agent_for_self_lifecycle()

    result = await _run(
        tmp_path,
        _self_lifecycle_code("ava.self.compact('audit e2e summary')"),
        database_gate=database_gate,
    )

    assert isinstance(result, ExecLifecycle)
    assert isinstance(result.exc, SystemHalt)
    content, kind, _source = _self_inbound_row()
    assert kind == "compact_summary"
    assert content == "audit e2e summary"


async def test_subprocess_unknown_lifecycle_class_crashes(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    result = await _run(
        tmp_path,
        (
            "from base.agents.lifecycle import LifecycleExit\n"
            "class _MysteryLifecycle(LifecycleExit):\n"
            "    def __init__(self):\n"
            "        super().__init__(0)\n"
            "raise _MysteryLifecycle()\n"
        ),
        database_gate=database_gate,
    )

    assert isinstance(result, ExecCrashed)
    assert isinstance(result.exc, ExecChildError)
    assert result.exc.exc_type == "unknown_lifecycle_class"
    assert result.exc.exc_msg == ("child reported unknown lifecycle class '_MysteryLifecycle'")


async def test_subprocess_kills_process_group_on_timeout(
    tmp_path: Path, database_gate: ProcessDbGate
) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, time\n"
        f"p = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    result = await _run(tmp_path, code, timeout=10.0, database_gate=database_gate)
    assert isinstance(result, ExecTimedOut)
    gc_pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(gc_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"grandchild pid {gc_pid} survived the exec process-group kill")
