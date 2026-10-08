"""A recording model for the schedule draft's real writer agent."""

from __future__ import annotations

from tests.e2e.fakes._recording import RecordingModel, say


def build(model: str, *, agent_id: int | None) -> RecordingModel:
    return RecordingModel(agent_id=agent_id, script=(say("schedule draft received"),))
