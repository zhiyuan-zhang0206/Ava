"""Renderer implementation failures propagate from their owning history component."""

from typing import NoReturn

import pytest
from langchain_core.messages import AIMessage

from base.agents.history import timeline
from base.agents.history.timeline_inputs import TimelineReadInputs


def test_dispatch_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_dispatch(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("simulated message dispatch failure")

    monkeypatch.setattr(timeline, "_ai_message_items", fail_dispatch)
    with pytest.raises(RuntimeError, match="simulated message dispatch failure"):
        timeline.build_timeline_items(
            [AIMessage(content="hello")],
            [],
            inputs=TimelineReadInputs(fail_dispatch, lambda: False),
        )
