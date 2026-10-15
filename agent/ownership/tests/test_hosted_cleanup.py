import asyncio
from typing import override

import pytest

from agent.ownership.hosted_cleanup import finish_termination
from base.events.live.bus import EventBus, EventBusConfig


class RecordingBus(EventBus):
    def __init__(self, effects: list[str], failure: BaseException | None) -> None:
        super().__init__(EventBusConfig(redis_url="redis://127.0.0.1:1/0", events_channel="test"))
        self.effects = effects
        self.failure = failure

    @override
    async def publish_best_effort(
        self, payload: str, *, channel: str | None = None, context: str = ""
    ) -> int | None:
        self.effects.append("publish")
        if self.failure is not None:
            raise self.failure
        return 1


@pytest.mark.parametrize("publication", ["ok", "error", "cancelled"])
@pytest.mark.parametrize("shell_fails", [False, True])
async def test_browser_cancellation_still_attempts_shell_and_preserves_first_failure(
    publication: str, shell_fails: bool
) -> None:
    effects: list[str] = []
    publication_error = {
        "ok": None,
        "error": RuntimeError("publication failed"),
        "cancelled": asyncio.CancelledError("publication cancelled"),
    }[publication]
    shell_error = RuntimeError("shell failed")
    entered, blocked = asyncio.Event(), asyncio.Event()
    browser_cancellations: list[asyncio.CancelledError] = []

    async def blocked_browser(agent_id: int) -> bool:
        assert agent_id == 42
        effects.append("browser")
        entered.set()
        try:
            await blocked.wait()
        except asyncio.CancelledError as exc:
            browser_cancellations.append(exc)
            raise
        raise AssertionError("the browser wait must be cancelled")

    def kill_shells(agent_id: int, cutoff: int) -> None:
        assert (agent_id, cutoff) == (42, 7)
        effects.append("shell")
        if shell_fails:
            raise shell_error

    task = asyncio.create_task(
        finish_termination(
            RecordingBus(effects, publication_error),
            42,
            7,
            kill_shells,
            page_releaser=blocked_browser,
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel("browser wait cancelled")
    expected = type(publication_error) if publication_error is not None else asyncio.CancelledError
    with pytest.raises(expected) as caught:
        await asyncio.wait_for(task, 2)
    assert effects == ["publish", "browser", "shell"]
    assert len(browser_cancellations) == 1
    assert caught.value is (publication_error or browser_cancellations[0])
    notes = getattr(caught.value, "__notes__", [])
    if publication_error is not None:
        assert any("Post-commit browser cleanup also failed" in note for note in notes)
    if shell_fails:
        assert any("Post-commit shell cleanup also failed" in note for note in notes)


async def test_ordinary_browser_failure_remains_best_effort_and_attempts_shell() -> None:
    effects: list[str] = []

    async def failed_browser(agent_id: int) -> bool:
        assert agent_id == 42
        effects.append("browser")
        raise RuntimeError("browser unavailable")

    def kill_shells(agent_id: int, cutoff: int) -> None:
        assert (agent_id, cutoff) == (42, 7)
        effects.append("shell")

    await finish_termination(
        RecordingBus(effects, None), 42, 7, kill_shells, page_releaser=failed_browser
    )
    assert effects == ["publish", "browser", "shell"]
