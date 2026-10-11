"""The concrete logger adapter feeds its original event writer and filter."""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from base import telemetry
from base.db import Database
from base.log import process as slog
from base.paths import logs_dir

_AGENT = 8901


@pytest.fixture
def logging_pipeline(database: Database) -> Iterator[None]:
    """The adapter test explicitly owns its native writer for its whole record."""
    pipeline = telemetry.build_pipeline(database=lambda: database)
    try:
        telemetry.init_telemetry(process="test-proc", pipeline=pipeline)
        yield
    finally:
        pipeline.stop(timeout=2)


def _mirror_last(event_name: str, agent: int) -> dict[str, Any]:
    telemetry.flush()
    day = datetime.now(UTC).strftime("%Y%m%d")
    rows = [
        row
        for line in (logs_dir() / f"events-{day}.jsonl").read_text().splitlines()
        if (row := json.loads(line)).get("event_name") == event_name
        and row.get("agent_id") == agent
    ]
    assert rows, f"expected a mirror line for {event_name}/{agent}, got none"
    return rows[-1]


def test_production_loguru_sink_routes_through_the_emitter() -> None:
    """`_event_sink` must enqueue into the emitter (which batches), not write
    straight to the DB per record — the regression that deleted batching before.
    """

    import inspect

    src = inspect.getsource(slog._event_sink)
    assert "telemetry.emit" in src
    wiring = inspect.getsource(slog.add_event_sink)
    assert "telemetry.init_telemetry" in wiring
    assert "_event_sink" in wiring  # the adapter is the loguru handler


def _rec(extra: dict[str, Any]) -> Any:
    """A minimal loguru-Record-shaped object — the filter reads only `extra`."""
    return {"extra": extra}


def test_add_event_sink_registers_at_most_once(database: Database) -> None:
    """Two `add_event_sink` calls (exec_child's env + request init paths)
    must not double-register the adapter: one loguru record lands exactly one
    mirror row. The 2026-08-24 double registration wrote every post-init
    record twice — byte-identical rows, same surrogate id — into the JSONL
    mirror (task #1638)."""
    from datetime import UTC as _UTC

    from base.log import logger as _g

    pipeline = telemetry.build_pipeline(database=lambda: database)
    first = slog.add_event_sink(
        process="test-dup-guard", producer=lambda: pipeline, machine_reader=lambda: "test-host"
    )
    second = slog.add_event_sink(
        process="test-dup-guard", producer=lambda: pipeline, machine_reader=lambda: "test-host"
    )
    assert second == first, "repeat registration must return the live sink id"

    marker = f"dup-guard-{time.time_ns()}"
    try:
        _g.configure(extra={"agent_id": "8901"})
        _g.info(f"one record {marker}", event="label_change")
        telemetry.flush()

        from base.paths import logs_dir

        day = datetime.now(_UTC).strftime("%Y%m%d")
        path = logs_dir() / f"events-{day}.jsonl"
        rows: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("attributes", {}).get("msg", "")
                if obj.get("event_name") == "label_change" and marker in msg:
                    rows.append(obj)
        assert len(rows) == 1, f"expected 1 mirror row, got {len(rows)}"
    finally:
        pipeline.stop(timeout=2)
        _g.remove(first)


def test_event_pipeline_filter_drops_no_emitter_and_node_enter() -> None:
    """The pipeline filter admits ordinary records but drops two families:
    the emitter's own failure reports (`_no_emitter` marker — a mirror-down
    process must not loop failure → warning → emit) and `node_enter` (zero
    event-stream consumers; the row would be pure write amplification — the
    log-file line is the death-analysis source). Anything else — including
    `node_exit`, which agent_inspect reads — passes."""

    f = slog._event_pipeline_filter
    assert f(_rec({"event": "node_exit"}))
    assert f(_rec({"event": "llm_usage"}))
    assert f(_rec({}))
    assert not f(_rec({"event": "node_enter"}))
    assert not f(_rec({"_no_emitter": True}))
    # The node_enter drop is by event name only, not by message — a record
    # that merely *mentions* node_enter in its message still passes.
    assert f(_rec({"msg": "node_enter handling"}))
    # The OTel SDK's post-end warning (a deliberate consequence of ending the
    # claim node span early in claim_idle_wait_span) is dropped from the
    # table; a record merely mentioning the text still passes.
    assert not f(cast(Any, dict(_rec({}), message="Setting attribute on ended span.")))
    assert f(cast(Any, dict(_rec({}), message="Setting attribute on ended span. (context)")))


def _rec_level(extra: dict[str, Any], level: str = "INFO") -> Any:
    """A loguru-Record-shaped dict with a level — the sampling branch reads it."""
    return {"extra": extra, "level": SimpleNamespace(name=level)}


def test_event_pipeline_filter_samples_info_log_one_in_ten() -> None:
    """Bare INFO `log` records are level-graded-sampled: exactly one in ten
    passes; the counter is per-process and resettable for tests."""
    f = slog._event_pipeline_filter
    slog._log_info_sample_counter = itertools.count()
    kept = [f(_rec_level({"event": "log"})) for _ in range(10)]
    assert kept.count(True) == 1
    assert kept[0] is True  # the first record of each block is the survivor


def test_event_pipeline_filter_keeps_warning_and_named_events() -> None:
    """WARNING+ bare logs and every named event pass unchanged — the alerts /
    unresolved surfaces depend on them; only bare INFO logs are sampled."""
    f = slog._event_pipeline_filter
    slog._log_info_sample_counter = itertools.count()
    assert f(_rec_level({"event": "log"}, "WARNING"))
    assert f(_rec_level({"event": "log"}, "ERROR"))
    assert f(_rec_level({"event": "log"}, "CRITICAL"))
    # named events (node_exit, llm_usage) are not bare logs
    assert f(_rec_level({"event": "node_exit"}))
    assert f(_rec_level({"event": "llm_usage"}))
    # A label is payload metadata and follows the ordinary bare-log sample rate.
    slog._log_info_sample_counter = itertools.count()
    assert sum(f(_rec_level({"label": "exec"})) for _ in range(10)) == 1
    # a record without a level passes unsampled (test-records / tolerant path)
    assert f(_rec({}))


@pytest.mark.usefixtures("logging_pipeline")
def test_loguru_adapter_sets_source_from_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`source=` on a logger call lands in the unified source field (default
    'system')."""
    from loguru import logger as _g

    from base.log.process import _event_sink

    sink_id = _g.add(_event_sink, level="INFO", enqueue=False, catch=False)
    try:
        _g.configure(extra={"agent_id": str(_AGENT)})
        _g.info("a line from the user path", event="label_change", source="user")
    finally:
        _g.remove(sink_id)
    obj = _mirror_last("label_change", _AGENT)
    assert obj["source"] == "user"
    assert obj["attributes"] == {"msg": "a line from the user path"}
    # loguru stamps record time in the machine's local zone; the emitter must
    # normalize it to UTC so the mirror carries one offset (task #1638).
    assert datetime.fromisoformat(obj["ts"]).utcoffset() == timedelta(0)
