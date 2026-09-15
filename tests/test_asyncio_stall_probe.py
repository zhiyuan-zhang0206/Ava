"""The CI stall probe really dumps asyncio chains before faulthandler does.

Task #3513: the backend shards stalled silently and the faulthandler thread
dumps could not name the stuck test. `tests/_asyncio_stall_probe.py` exists to
close that gap. This meta-test proves the instrument is wired into the session,
fires inside a real pytest run under `-o faulthandler_timeout=N` with the chain
dump landing BEFORE the thread dump, and stays inert — nothing patched —
without a deadline. A probe that silently stopped arming would leave the next
stall as blind as the ones that motivated it; treat a red here as a blocking
regression of the forensics, not as a flaky meta-test.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import cast

import pytest

from tests import _asyncio_stall_probe as probe

_REPO_ROOT = Path(__file__).resolve().parent.parent

_SYNTHETIC_HANG = """\
import asyncio


async def test_hangs_forever() -> None:
    await asyncio.Event().wait()
"""


class _StubConfig:
    """The only `pytest.Config` surface `faulthandler_timeout` reads."""

    def __init__(self, value: float | str | None, *, unknown_ini: bool = False) -> None:
        self._value = value
        self._unknown_ini = unknown_ini

    def getini(self, name: str) -> float | str | None:
        if self._unknown_ini:
            raise ValueError(name)
        return self._value


def test_faulthandler_timeout_reads_the_ini_and_defaults_to_zero() -> None:
    assert probe.faulthandler_timeout(cast("pytest.Config", _StubConfig(300))) == 300.0
    assert probe.faulthandler_timeout(cast("pytest.Config", _StubConfig(None))) == 0.0
    disabled_plugin = _StubConfig(None, unknown_ini=True)
    assert probe.faulthandler_timeout(cast("pytest.Config", disabled_plugin)) == 0.0


def test_awaiter_chain_lines_walk_to_the_innermost_await() -> None:
    loop = asyncio.new_event_loop()
    task: asyncio.Task[None] | None = None
    try:

        async def innermost() -> None:
            await asyncio.sleep(30)

        async def outermost() -> None:
            await innermost()

        task = loop.create_task(outermost())
        deadline = time.monotonic() + 10.0
        lines: list[str] = []
        while time.monotonic() < deadline:
            lines = probe.awaiter_chain_lines(task)
            if any(" in sleep" in line for line in lines):
                break
            loop.run_until_complete(asyncio.sleep(0))
        assert any(" in outermost" in line for line in lines), lines
        assert any(" in innermost" in line for line in lines), lines
        assert lines[-1].endswith(" in sleep"), lines
    finally:
        if task is not None:
            task.cancel()
            loop.run_until_complete(asyncio.gather(task, return_exceptions=True))
        loop.close()


def test_probe_window_without_a_deadline_patches_nothing() -> None:
    with tempfile.TemporaryFile() as sink:
        events_original = asyncio.events.new_event_loop
        top_original = asyncio.new_event_loop
        with probe.probe_window("tests/synthetic.py::test_x", 0.0, sink.fileno()):
            assert asyncio.events.new_event_loop is events_original
            assert asyncio.new_event_loop is top_original
        assert asyncio.events.new_event_loop is events_original
        assert asyncio.new_event_loop is top_original


def test_probe_window_dumps_pending_chains_when_the_deadline_nears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "DUMP_MARGIN_SECONDS", 0.1)
    with tempfile.TemporaryFile() as sink:
        events_original = asyncio.events.new_event_loop
        top_original = asyncio.new_event_loop
        with probe.probe_window("tests/synthetic.py::test_hangs", 0.5, sink.fileno()):
            assert asyncio.events.new_event_loop is not events_original
            assert asyncio.new_event_loop is not top_original
            loop = asyncio.new_event_loop()
            try:
                # Bounded wait: the timer is due 0.4s after the loop's creation,
                # so a handful of iterations are enough even on a loaded runner.
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline and os.fstat(sink.fileno()).st_size == 0:
                    loop.run_until_complete(asyncio.sleep(0.02))
            finally:
                loop.close()
        sink.seek(0)
        dumped = sink.read().decode()
    assert "tests/synthetic.py::test_hangs" in dumped, dumped
    assert "--- task " in dumped, dumped
    assert " in sleep" in dumped, dumped
    assert asyncio.events.new_event_loop is events_original
    assert asyncio.new_event_loop is top_original


def test_probe_window_cancels_its_timers_at_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe, "DUMP_MARGIN_SECONDS", 0.1)
    with tempfile.TemporaryFile() as sink:
        with probe.probe_window("tests/synthetic.py::test_healthy", 0.5, sink.fileno()):
            loop = asyncio.new_event_loop()  # armed; its timer is due 0.4s from now
        # The window is gone and the loop never ran inside it. Letting the loop
        # run past the timer's due time must not dump: `_pytest.faulthandler`
        # gives its own dump the same cancel-at-test-end lifetime.
        try:
            loop.run_until_complete(asyncio.sleep(0.5))
        finally:
            loop.close()
        sink.seek(0)
        assert sink.read() == b""


def test_the_probe_hook_is_wired_into_this_session(pytestconfig: pytest.Config) -> None:
    assert pytestconfig.pluginmanager.hasplugin("tests._asyncio_stall_probe"), (
        "tests/conftest.py no longer loads tests/_asyncio_stall_probe via pytest_plugins"
    )
    impls = pytestconfig.hook.pytest_runtest_protocol.get_hookimpls()
    wired = [
        impl.function
        for impl in impls
        if impl.function is probe.pytest_runtest_protocol and impl.wrapper
    ]
    assert wired, "the stall-probe protocol wrapper is not registered in this session"


def test_the_probe_dumps_before_faulthandler_in_a_real_pytest_run(tmp_path: Path) -> None:
    (tmp_path / "test_probe_e2e.py").write_text(_SYNTHETIC_HANG, encoding="utf-8")
    env = dict(os.environ)
    # A stray addopts (say a local `-n 4 --splits 16`) would re-shape the inner run.
    env.pop("PYTEST_ADDOPTS", None)
    proc = subprocess.run(  # noqa: S603 — our own interpreter, fixed repo paths
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path),
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests._asyncio_stall_probe",
            "-o",
            "asyncio_mode=auto",
            "-o",
            "faulthandler_timeout=4",
            # End the inner process at its own deadline instead of letting it
            # hang into this test's timeout: the dump under test fired by then.
            "-o",
            "faulthandler_exit_on_timeout=true",
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    probe_at = combined.find("asyncio stall probe")
    thread_dump_at = combined.find("Timeout (")
    assert probe_at != -1, "the probe never dumped:\n" + combined[-3000:]
    assert thread_dump_at != -1, "faulthandler never dumped:\n" + combined[-3000:]
    assert probe_at < thread_dump_at, (
        "the chain dump must land before the thread dump:\n" + combined[-3000:]
    )
    assert "test_probe_e2e.py::test_hangs_forever" in combined, combined[-3000:]
    assert " in wait" in combined, combined[-3000:]
