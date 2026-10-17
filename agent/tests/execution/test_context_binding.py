from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import meiva
from agent.graph.exec.protocol import (
    ResultPayload,
    make_request_path,
    make_result_path,
    read_result,
    write_request,
)
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.sdk.tally import SdkCallTally
from base.packages.plugin_providers import build_model_catalog
from meiva.boot import process_clients


@patch.dict(os.environ)
def _start(tmp_path: Path, agent_id: int, code: str) -> tuple[subprocess.Popen[str], Path]:
    context = MeivaContext(
        identity=AgentIdentity(agent_id, True),
        catalog=build_model_catalog(),
        clients=process_clients(),
    )
    request = make_request_path(tmp_path / "exec", agent_id=agent_id)
    result = make_result_path(tmp_path / "exec", agent_id=agent_id)
    write_request(
        request,
        code=code,
        context=context.describe(),
        timeout_s=30,
        state={"messages": [], "halted": False},
        incarnation=None,
    )
    env = os.environ | {
        "MEIVA_HOME": str(tmp_path / "home"),
        "MEIVA_AGENT_ID": str(agent_id),
        "MEIVA_PROCESS_PROFILE": "agent",
        "MEIVA_EXEC_REQUEST_FILE": str(request),
        "MEIVA_EXEC_RESULT_FILE": str(result),
    }
    return (
        subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", "-m", "agent.execution.child"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        ),
        result,
    )


def _finish(process: subprocess.Popen[str], result: Path) -> tuple[str, ResultPayload]:
    try:
        stdout, stderr = process.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise
    assert process.returncode == 0, stderr
    return stdout, read_result(result)


def test_concurrent_children_bind_imports_threads_and_state_independently(tmp_path: Path) -> None:
    original = getattr(meiva, "context", None)
    first, first_result = _start(
        tmp_path / "one",
        101,
        "import json, os, time, threading\n"
        "import meiva\nfrom meiva import context, state, state_update\n"
        "from concurrent.futures import ThreadPoolExecutor\n"
        "with ThreadPoolExecutor(2) as pool:\n"
        "    seen = list(pool.map(lambda _: [meiva.context is context, meiva.state is state, "
        "meiva.state_update is state_update, meiva.self.AGENT_ID], range(4)))\n"
        "state_update['halted'] = True\n"
        "time.sleep(0.2)\n"
        "print(json.dumps([os.getpid(), seen, threading.Thread.start.__module__]))\n",
    )
    second, second_result = _start(
        tmp_path / "two",
        202,
        "import meiva, os\nprint(os.getpid(), meiva.self.AGENT_ID, meiva.state.halted)",
    )
    try:
        first_stdout, first_payload = _finish(first, first_result)
        second_stdout, second_payload = _finish(second, second_result)
    finally:
        for process in (first, second):
            if process.poll() is None:
                process.kill()
                process.communicate()
    first_pid, seen, thread_owner = json.loads(first_stdout.strip().splitlines()[-1])
    second_pid, second_id, second_halted = second_stdout.strip().splitlines()[-1].split()
    assert first_payload.kind == second_payload.kind == "done"
    assert first_pid != int(second_pid) != os.getpid()
    assert seen == [[True, True, True, 101]] * 4
    assert thread_owner == "threading"
    assert (second_id, second_halted) == ("202", "False")
    assert first_payload.state_update == {"halted": True}
    assert second_payload.state_update is None
    assert getattr(meiva, "context", None) is original


@pytest.mark.parametrize(
    "failure", ["ValueError('original source')", "KeyboardInterrupt()", "TimeoutError()"]
)
def test_failure_preserves_legal_delta_and_original_traceback(tmp_path: Path, failure: str) -> None:
    code = f"import meiva\nmeiva.state_update['halted'] = True\nraise {failure}\n"
    process, result = _start(tmp_path, 303, code)
    stdout, payload = _finish(process, result)
    assert payload.state_update == {"halted": True}
    expected = (
        "crashed"
        if failure.startswith("ValueError")
        else ("cancelled" if failure.startswith("Keyboard") else "timed_out")
    )
    assert payload.kind == expected
    if expected == "crashed":
        assert 'File "<agent_code>", line 3' in stdout
        assert f"raise {failure}" in stdout
        assert f"raise {failure}" in (payload.full_traceback or "")


@patch.dict(os.environ)
def test_execution_tally_stays_with_its_context_and_out_of_description() -> None:
    tally = SdkCallTally()
    context = MeivaContext(
        identity=AgentIdentity(41, True), clients=process_clients(), sdk_calls=tally
    )
    tally.add("files.read")
    assert set(context.describe()) == {"identity", "gateway_url"}
    assert MeivaContext().sdk_calls is None
    assert context.sdk_calls is tally
    assert tally.snapshot() == {"files.read": 1}


@patch.dict(os.environ)
def test_child_sdk_tally_includes_plain_thread_calls(tmp_path: Path) -> None:
    sample = tmp_path / "thread-sample.txt"
    sample.write_text("hello", encoding="utf-8")
    code = f"""
import meiva
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=4) as workers:
    values = list(workers.map(lambda _: meiva.files.read({str(sample)!r}), range(24)))
assert len(values) == 24
"""
    from agent.tests.execution.test_exec_child import spawn_real_child

    proc, _request, result = spawn_real_child(tmp_path, code)
    assert proc.returncode == 0, proc.stderr
    payload = read_result(result)
    assert payload.kind == "done", payload.exc_msg
    assert payload.sdk_calls == [{"method": "files.read", "count": 24}]


def test_controlled_child_plugin_faces_keep_its_context_writer(tmp_path: Path) -> None:
    code = """
import meiva
from meiva.sdk_surface.installation import installed
context_writer = meiva.context.clients.event_pipeline()
surface = installed()
assert surface is not None and surface.producer is not None
assert surface.producer() is context_writer
meiva.ensure_plugins_loaded(surface=False)
faces = installed()
assert faces is not None and faces.faces and faces.producer is not None
assert faces.producer() is context_writer
print("owned writer preserved")
"""
    process, result = _start(tmp_path, 515, code)
    stdout, payload = _finish(process, result)
    assert payload.kind == "done", payload.exc_msg
    assert "owned writer preserved" in stdout
