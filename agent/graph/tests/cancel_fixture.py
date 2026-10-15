import pytest

from agent.graph.exec.tests.cancel_fixture import install_cancel_event as install_exec
from agent.graph.hooks.tests.cancel_fixture import install_cancel_event as install_hooks
from agent.graph.interrupt import InterruptEvent
from agent.graph.llm.tests.cancel_fixture import install_cancel_event as install_llm


@pytest.fixture
def fake_cancel_event(monkeypatch: pytest.MonkeyPatch) -> InterruptEvent:
    event = InterruptEvent()
    install_llm(monkeypatch, event)
    install_exec(monkeypatch, event)
    install_hooks(monkeypatch, event)
    return event
