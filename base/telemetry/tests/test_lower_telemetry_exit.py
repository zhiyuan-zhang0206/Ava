"""Real process exit hooks keep the cold path cold and stop loaded owners."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _child(program: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["AVA_HOME"] = str(home)
    return subprocess.run(  # noqa: S603 — fixed interpreter and test-owned inline program
        [sys.executable, "-I", "-c", program],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_cold_exit_does_not_import_or_start_lower_exporters(tmp_path: Path) -> None:
    child = _child(
        """
import atexit
import sys
import threading

def observe():
    assert 'base.telemetry.tracing' not in sys.modules
    assert 'base.telemetry.otlp.telemetry_otlp' not in sys.modules
    assert 'opentelemetry.sdk' not in sys.modules
    assert not any(t.name in {'trace-arm', 'trace-collector-retry', 'otlp-exporter'}
                   for t in threading.enumerate())
    sys.stdout.write('cold exit observed\\n')

atexit.register(observe)
from base import telemetry
assert telemetry._state['pipeline'] is None
""",
        tmp_path / "cold-home",
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout == "cold exit observed\n", child.stderr
    assert "Exception ignored in atexit callback" not in child.stderr


def test_loaded_trace_owner_is_stopped_by_its_registered_exit_hook(tmp_path: Path) -> None:
    child = _child(
        """
import atexit
import sys
import threading

owners = []

def observe():
    from base.telemetry import tracing
    assert tracing._state['closed']
    assert len(owners) == 1
    assert owners[0].finished.is_set()
    assert not owners[0]._thread.is_alive()
    sys.stdout.write('loaded trace exit observed\\n')

atexit.register(observe)
from base.telemetry import tracing
entered = threading.Event()

def arm(endpoint, stop_requested):
    entered.set()
    stop_requested.wait()

tracing._arm_tracing = arm
tracing._start_arm_thread('http://local-test-collector')
assert entered.wait(5)
owners.append(tracing._state['arm_owner'])
""",
        tmp_path / "hot-home",
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout == "loaded trace exit observed\n", child.stderr
    assert "Exception ignored in atexit callback" not in child.stderr
