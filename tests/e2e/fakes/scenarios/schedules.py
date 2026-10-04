"""A recording model for the schedule draft's real writer agent."""

from __future__ import annotations

from tests.e2e.fakes._recording import RecordingModel, say


def build(model: str) -> RecordingModel:
    return RecordingModel(script=(say("schedule draft received"),))
