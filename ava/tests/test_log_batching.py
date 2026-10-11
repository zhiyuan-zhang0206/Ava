"""Standalone SDK startup retains immediate telemetry overflow diagnostics."""

from pathlib import Path

_CHILD = Path(__file__).parent / "bootstrap" / "child_sdk_probes.py"


def test_standalone_overflow_reports_before_logging_or_metrics_initialize() -> None:
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603 — fixed isolated diagnostic regression
        [sys.executable, str(_CHILD), "telemetry_overflow"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert "ERROR: Telemetry queue emitter full: lost 1 event(s)" in result.stderr
