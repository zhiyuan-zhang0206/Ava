"""Native terminal survival, complete Job closure, and unresolved-owner evidence."""

import json
import subprocess
import sys
from contextlib import suppress
from typing import cast

import psutil
import pytest

from shared.windows_terminal.record import TerminalRecord, read
from tests.lifecycle.native_root.test_windows_root import (
    _root,
    ended,
    root_fixture,
    sleeping_service,
    wait_for,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows terminal Job proof")


def _read_record(name: str) -> TerminalRecord:
    record = read(name)
    assert record is not None
    return record


@pytest.fixture
def terminal_backend(native_env, monkeypatch):
    from shared.config import settings
    from shared.windows_terminal.backend import WindowsTerminalBackend

    monkeypatch.setattr(settings.general, "ava_home", native_env["AVA_HOME"])
    monkeypatch.setattr(settings.general, "cluster_registry", native_env["AVA_CLUSTER_REGISTRY"])
    return WindowsTerminalBackend()


def test_terminal_brokered_from_service_survives_root_exit_then_closes_complete_job(
    tmp_path, native_env, terminal_backend
):
    descendant = tmp_path / "terminal-child"
    terminal_code = sleeping_service(descendant)
    command = subprocess.list2cmdline([sys.executable, "-u", "-c", terminal_code])
    birth_receipt = tmp_path / "terminal-birth"
    # The requester is itself inside an application Job. Only the explicit
    # resource call reaches root; no breakaway/local-spawn route is available.
    service_code = f"""import os,signal,time,pathlib,json
from shared.windows_terminal.backend import WindowsTerminalBackend
signal.signal(signal.SIGBREAK, lambda *args: exit(0))
WindowsTerminalBackend().new_session('native-terminal', {command!r}, pathlib.Path({str(tmp_path)!r}), env=dict(os.environ))
pathlib.Path({str(birth_receipt)!r}).write_text('ready')
while True: time.sleep(0.02)
"""
    try:
        with root_fixture(tmp_path, native_env, service_code, terminal_broker=True) as (
            root,
            client,
            _,
            _,
        ):
            wait_for(
                birth_receipt.exists, "service could not obtain a brokered terminal", timeout=25
            )
            wait_for(descendant.exists, "terminal descendant did not start")
            member = psutil.Process(int(descendant.read_text()))
            before = _read_record("native-terminal")
            assert before.state == "running"
            assert before.owner is not None
            assert before.root.pid == cast(int, _root(client.status())["pid"])
            assert before.owner.identity().live()
            assert terminal_backend.new_session(
                "native-terminal", command, tmp_path, env=native_env
            )
            assert _read_record("native-terminal").domain == before.domain
            assert client.shutdown()["ok"]
            assert root.wait(timeout=15) == 0
            assert before.owner.identity().live()
            assert member.is_running()
            assert terminal_backend.kill_session("native-terminal", graceful=True, timeout=15)[0]
            wait_for(lambda: ended(member), "terminal close left a Job member")
            receipt = _read_record("native-terminal")
            assert receipt.state == "closed" and receipt.empty_job_observed
            assert not terminal_backend.list_sessions()
    finally:
        with suppress(Exception):
            terminal_backend.kill_session("native-terminal", graceful=False)


def test_terminal_owner_death_closes_members_without_fabricating_receipt(
    tmp_path, native_env, terminal_backend
):
    from shared.windows_terminal.record import record_path

    descendant = tmp_path / "terminal-child"
    command = subprocess.list2cmdline([sys.executable, "-u", "-c", sleeping_service(descendant)])
    with root_fixture(
        tmp_path,
        native_env,
        "import time; time.sleep(120)",
        ignore_break=True,
        terminal_broker=True,
    ):
        assert terminal_backend.new_session("lost-terminal", command, tmp_path, env=native_env)
        wait_for(descendant.exists, "terminal descendant did not start")
        member = psutil.Process(int(descendant.read_text()))
        original = _read_record("lost-terminal")
        assert original.owner is not None
        assert original.owner.identity().live()
        psutil.Process(original.owner.pid).kill()
        wait_for(lambda: ended(member), "owner death left native Job members")
        assert json.loads(record_path("lost-terminal").read_text())["state"] != "closed"
        with pytest.raises(RuntimeError, match="custody requires reconciliation"):
            terminal_backend.kill_session("lost-terminal", graceful=False)
        with pytest.raises(RuntimeError, match="custody requires reconciliation"):
            terminal_backend.new_session("lost-terminal", command, tmp_path, env=native_env)
        assert _read_record("lost-terminal").domain == original.domain
