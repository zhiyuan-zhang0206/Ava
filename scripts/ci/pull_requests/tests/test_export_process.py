"""Standalone exporters retain the entry image, gate and actual writer errors."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_settings
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import Event, EventPipeline
from scripts.ci.pull_requests import export_process, runs_export


@pytest.mark.parametrize("sha", ["a" * 40, None])
def test_writer_uses_one_captured_gate_and_is_joined(
    monkeypatch: pytest.MonkeyPatch, sha: str | None
) -> None:
    image = LoadedCommit(Path("/loaded-exporter"), sha)
    gates: list[ProcessDbGate] = []
    counts: list[tuple[Path, str]] = []

    def count(repo: Path, rev: str = "HEAD") -> int:
        counts.append((repo, rev))
        return 22

    def handle(*, gate: ProcessDbGate) -> Database:
        gates.append(gate)
        return Database(db_config_from_settings(), gate=gate)

    def build(*, database: Callable[[], Database]) -> EventPipeline:
        database()
        database()
        return EventPipeline(writer=lambda _events: None)

    monkeypatch.setattr(Database, "from_settings", handle)
    monkeypatch.setattr(code_version, "first_parent_count", count)
    monkeypatch.setattr(export_process, "build_pipeline", build)
    with export_process.owned_event_pipeline("exporter", image=image) as pipeline:
        assert not pipeline.stopped
        assert len(gates) == 2 and gates[0] is gates[1]
        if sha is None:
            with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
                gates[0].application_name()
            assert counts == []
        else:
            assert gates[0].application_name() == gates[1].application_name() == "ava:exporter:v22"
            assert counts == [(image.source_root, sha)]
    assert pipeline.stopped


def test_body_error_preserves_the_original_writer_error_during_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = ValueError("export failed")
    worker_error = OSError("writer failed")

    def write(_events: list[Event]) -> None:
        raise worker_error

    pipeline = EventPipeline(writer=write, batch_size=1)

    def build(*, database: Callable[[], Database]) -> EventPipeline:
        assert callable(database)
        return pipeline

    monkeypatch.setattr(export_process, "build_pipeline", build)
    event = Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster="test",
        process="exporter",
        category="telemetry",
        event_name="pr_flow_run",
        level="info",
        source="test",
        target_agent_id=None,
    )
    with (
        pytest.raises(ValueError) as caught,
        export_process.owned_event_pipeline(
            "exporter", image=LoadedCommit(Path("/loaded"), None)
        ) as owned,
    ):
        owned.enqueue(event)
        raise primary
    assert caught.value is primary and pipeline.stopped
    assert any("writer failed" in note for note in primary.__notes__)
    with pytest.raises(OSError) as worker:
        pipeline.stop(timeout=0)
    assert worker.value is worker_error


@pytest.mark.parametrize("borrowed", [False, True])
def test_export_entry_captures_before_fetch_or_borrows_the_supplied_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, borrowed: bool
) -> None:
    calls: list[str] = []
    image = LoadedCommit(Path("/loaded-exporter"), None)
    pipeline = EventPipeline(writer=lambda _events: None)

    def capture(source_root: Path) -> LoadedCommit:
        assert not borrowed
        calls.append("capture")
        return image

    def collect(
        repo: str, days: list[date], now: datetime, tz: ZoneInfo, state_dir: Path
    ) -> runs_export.RepoCollection:
        assert calls == ([] if borrowed else ["capture"])
        calls.append("fetch")
        return runs_export.RepoCollection(
            repo=repo,
            cache={},
            daily={},
            workflows={},
            window_runs=0,
            window_prs=0,
            api_requests=1,
        )

    def build(*, database: Callable[[], Database]) -> EventPipeline:
        assert not borrowed and callable(database)
        calls.append("build")
        return pipeline

    def emit(snapshot: dict[str, Any], *, pipeline: EventPipeline) -> None:
        assert "owner/repo" in snapshot["repositories"]
        calls.append("emit")
        assert not pipeline.stopped

    monkeypatch.setattr(LoadedCommit, "capture", capture)
    monkeypatch.setattr(runs_export, "collect_repo", collect)
    monkeypatch.setattr(runs_export, "save_json", Mock())
    monkeypatch.setattr(runs_export, "emit_snapshot", emit)
    monkeypatch.setattr(export_process, "build_pipeline", build)
    try:
        assert (
            runs_export.main(
                ["--repo", "owner/repo", "--state-dir", str(tmp_path)],
                producer=(lambda: pipeline) if borrowed else None,
            )
            == 0
        )
        assert calls == (["fetch", "emit"] if borrowed else ["capture", "fetch", "build", "emit"])
        assert pipeline.stopped is not borrowed
    finally:
        pipeline.stop(timeout=2)
