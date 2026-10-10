"""Standalone SDK startup retains immediate telemetry overflow diagnostics."""


def test_standalone_overflow_reports_before_logging_or_metrics_initialize() -> None:
    import subprocess
    import sys

    code = """
import queue, threading
import ava
from base import telemetry
from datetime import UTC, datetime
pipe = telemetry._EventPipeline.__new__(telemetry._EventPipeline)
pipe._queue = queue.Queue(maxsize=1)
pipe._admission_lock = threading.Lock()
pipe._stop_requested = threading.Event()
pipe._finished = threading.Event()
pipe._dropped_lock = threading.Lock()
pipe.dropped = 0
pipe._drop_example = None
event = telemetry.Event(ts=datetime.now(UTC), trace_id=None, span_id=None,
    agent_id=None, machine='test', cluster='test', process='standalone',
    category='telemetry', event_name='sdk_call', level='info', source='system',
    target_agent_id=None)
pipe.enqueue(event)
pipe.enqueue(event)
"""
    result = subprocess.run(  # noqa: S603 — fixed isolated diagnostic regression
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30, check=True
    )
    assert "ERROR: Telemetry queue emitter full: lost 1 event(s)" in result.stderr
