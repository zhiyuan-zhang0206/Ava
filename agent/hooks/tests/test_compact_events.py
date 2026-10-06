"""Compaction producers and raw event readers share two separate vocabularies."""

from __future__ import annotations

import json
from typing import Any, get_type_hints
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from agent.hooks.compact_events import emit_compact_finished, emit_compact_started
from base.events.live.projection import (
    EVENT_ADAPTER,
    CompactFinished,
    CompactionMode,
    CompactionStatus,
    CompactStarted,
)


def test_producer_and_projection_annotations_have_one_owner_per_dimension() -> None:
    assert get_type_hints(emit_compact_started)["mode"] is CompactionMode
    assert CompactStarted.model_fields["mode"].annotation is CompactionMode
    assert get_type_hints(emit_compact_finished)["status"] is CompactionStatus
    assert CompactFinished.model_fields["status"].annotation is CompactionStatus
    assert {member.value for member in CompactionMode} == {"auto", "request"}
    assert {member.value for member in CompactionStatus} == {"success", "failure", "replaced"}


@pytest.mark.parametrize("mode", list(CompactionMode))
@pytest.mark.parametrize("status", list(CompactionStatus))
def test_each_compaction_pair_serializes_and_restores_existing_wire_values(
    mode: CompactionMode,
    status: CompactionStatus,
) -> None:
    publisher = MagicMock()
    run_id = emit_compact_started(publisher, 7, mode=mode)
    assert run_id is not None
    emit_compact_finished(publisher, 7, run_id, status=status)
    started_raw, finished_raw = [call.args[0] for call in publisher.emit.call_args_list]
    started = EVENT_ADAPTER.validate_json(started_raw)
    finished = EVENT_ADAPTER.validate_json(finished_raw)
    assert isinstance(started, CompactStarted) and started.mode is mode
    assert isinstance(finished, CompactFinished) and finished.status is status
    assert json.loads(started_raw)["mode"] == mode.value
    assert json.loads(finished_raw)["status"] == status.value
    assert started.compact_id == finished.compact_id == run_id


@pytest.mark.parametrize("invalid", [None, "unknown", "", 3, {}])
@pytest.mark.parametrize(
    "role,field", [("compact_started", "mode"), ("compact_finished", "status")]
)
def test_raw_event_domain_rejects_unknown_values(role: str, field: str, invalid: Any) -> None:
    raw = {
        "agent_id": 7,
        "role": role,
        "compact_id": "run1",
        "started_at": "now",
        "finished_at": "later",
        field: invalid,
    }
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(raw)


def test_absent_publisher_does_not_start_a_live_run() -> None:
    assert emit_compact_started(None, 7, mode=CompactionMode.REQUEST) is None
    publisher = MagicMock()
    emit_compact_finished(publisher, 7, None, status=CompactionStatus.REPLACED)
    publisher.emit.assert_not_called()
