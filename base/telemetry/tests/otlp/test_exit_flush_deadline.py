from __future__ import annotations

import inspect

import pytest

from base.telemetry.otlp import telemetry_otlp, telemetry_otlp_exporting


def test_remaining_time_hands_a_stage_only_the_real_remainder() -> None:
    assert telemetry_otlp.remaining_time(10.0, 10.0) == 0.0
    assert telemetry_otlp.remaining_time(10.0, 10.5) == 0.0
    assert telemetry_otlp.remaining_time(10.0, 9.999) == pytest.approx(0.001)
    assert telemetry_otlp.remaining_time(12.5, 10.0) == 2.5


def test_the_exit_flush_takes_no_clock_seam() -> None:
    for stop in (
        telemetry_otlp_exporting.ExportingEventPipeline.stop,
        telemetry_otlp.OtlpBackend.shutdown,
    ):
        assert list(inspect.signature(stop).parameters) == ["self", "timeout"], stop.__qualname__
