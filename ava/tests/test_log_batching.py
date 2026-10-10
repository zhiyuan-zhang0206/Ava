"""Standalone SDK startup retains immediate telemetry overflow diagnostics."""


def test_standalone_overflow_reports_before_logging_or_metrics_initialize() -> None:
    import subprocess
    import sys

    code = """
import threading
import ava
from base import telemetry
from datetime import UTC, datetime
entered = threading.Event()
release = threading.Event()
def write(events):
    entered.set()
    assert release.wait(5)
pipe = telemetry.EventPipeline(writer=write, batch_size=1, queue_maxsize=1)
event = telemetry.Event(ts=datetime.now(UTC), trace_id=None, span_id=None,
    agent_id=None, machine='test', cluster='test', process='standalone',
    category='telemetry', event_name='sdk_call', level='info', source='system',
    target_agent_id=None)
try:
    pipe.enqueue(event)
    assert entered.wait(5)
    pipe.enqueue(event)
    pipe.enqueue(event)
finally:
    release.set()
    assert pipe.stop(timeout=5).status is telemetry.DrainStatus.COMPLETED
"""
    result = subprocess.run(  # noqa: S603 — fixed isolated diagnostic regression
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=True
    )
    assert "ERROR: Telemetry queue emitter full: lost 1 event(s)" in result.stderr
