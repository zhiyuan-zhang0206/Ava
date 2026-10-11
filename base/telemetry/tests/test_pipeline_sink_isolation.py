"""A failed metric projection preserves the pipeline's other sinks and diagnostics."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from base import paths, telemetry
from base.db import Database
from base.telemetry.metrics import observed_metrics as metrics
from base.telemetry.otlp import telemetry_otlp


def test_projection_failure_keeps_jsonl_and_otlp_and_never_emits_recursively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database: Database
) -> None:
    metrics.project_events(database, [])
    monkeypatch.setattr(paths, "logs_dir", lambda: tmp_path)
    monkeypatch.setattr(metrics, "write_observations", Mock(side_effect=RuntimeError("DB down")))
    diagnostic = Mock()
    monkeypatch.setattr(telemetry, "report_no_pipeline", diagnostic)
    emitted = Mock(side_effect=AssertionError("diagnostics must bypass emitter"))
    monkeypatch.setattr(telemetry, "emit", emitted)
    exported = Mock()
    monkeypatch.setattr(telemetry_otlp, "export_batch", exported)
    event = telemetry.Event(
        ts=datetime(2026, 9, 15, 23, 59, 59, tzinfo=UTC),
        trace_id=None,
        span_id=None,
        agent_id=71,
        machine="test",
        cluster="test",
        process="test",
        category="telemetry",
        event_name="turn_end",
        level="info",
        source="system",
        target_agent_id=None,
        attributes={"ok": True, "duration_seconds": 2.5},
    )
    pipeline = telemetry.build_pipeline(database=lambda: database)
    try:
        telemetry.emit_prepared(event, producer=lambda: pipeline)
        assert pipeline.flush().status is telemetry.DrainStatus.COMPLETED
    finally:
        assert pipeline.stop(timeout=2).status is telemetry.DrainStatus.COMPLETED
    exported.assert_called_once_with([event])
    assert len(list(tmp_path.glob("events-*.jsonl"))) >= 1
    assert (
        str(telemetry.event_row(event)["id"]) in next(tmp_path.glob("events-*.jsonl")).read_text()
    )
    diagnostic.assert_called_once()
    emitted.assert_not_called()
