"""Clean-interpreter exit scenarios for the lower telemetry owners; never imported by a production entry.

Run as ``python -I _lower_exit_child.py <cold|loaded>`` with ``AVA_HOME`` set by the parent.
"""

from __future__ import annotations

import atexit
import sys
import threading
from pathlib import Path


def cold() -> None:
    def observe() -> None:
        assert "base.telemetry.tracing" not in sys.modules
        assert "base.telemetry.otlp.telemetry_otlp" not in sys.modules
        assert "opentelemetry.sdk" not in sys.modules
        assert not any(
            t.name in {"trace-arm", "trace-collector-retry", "otlp-exporter"}
            for t in threading.enumerate()
        )
        sys.stdout.write("cold exit observed\n")

    atexit.register(observe)
    from base import telemetry

    assert telemetry.process_state["pipeline"] is None


def loaded() -> None:
    owners: list[object] = []

    def observe() -> None:
        from base.telemetry import tracing

        assert tracing._state["closed"]
        assert len(owners) == 1
        assert owners[0].finished.is_set()  # type: ignore[attr-defined]
        assert not owners[0]._thread.is_alive()  # type: ignore[attr-defined]
        sys.stdout.write("loaded trace exit observed\n")

    atexit.register(observe)
    from base.telemetry import tracing
    from base.telemetry.tests.otlp.identity_inputs import machine_identity

    entered = threading.Event()

    def arm(endpoint: str, stop_requested: threading.Event, **_identity: object) -> None:
        entered.set()
        stop_requested.wait()

    tracing._arm_tracing = arm  # type: ignore[assignment]
    tracing.start_arm_thread("http://local-test-collector", machine_identity(), Path(".ava"))
    assert entered.wait(5)
    owners.append(tracing._state["arm_owner"])


if __name__ == "__main__":
    {"cold": cold, "loaded": loaded}[sys.argv[1]]()
