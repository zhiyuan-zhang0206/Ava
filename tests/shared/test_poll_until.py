from __future__ import annotations

import pytest

from tests.shared.poll_until import poll_until, poll_until_async


def test_poll_until_returns_when_condition_is_immediately_true() -> None:
    assert poll_until(lambda: True) is None


def test_poll_until_reports_last_state_on_timeout() -> None:
    with pytest.raises(AssertionError) as error:
        poll_until(
            lambda: (False, {"status": "still running"}),
            timeout=0.0,
            what="process exits",
        )

    message = str(error.value)
    assert message.startswith("Timed out after ")
    assert "waiting for process exits" in message
    assert "{'status': 'still running'}" in message


def test_poll_until_accepts_success_with_state() -> None:
    assert poll_until(lambda: (True, "ready")) is None


async def test_poll_until_async_returns_when_condition_becomes_true() -> None:
    observations = iter([False, True])

    assert await poll_until_async(lambda: next(observations), interval=0.0) is None


async def test_poll_until_async_reports_last_state_on_timeout() -> None:
    with pytest.raises(AssertionError) as error:
        await poll_until_async(
            lambda: (False, {"status": "still running"}),
            timeout=0.0,
            what="async process exits",
        )

    message = str(error.value)
    assert message.startswith("Timed out after ")
    assert "waiting for async process exits" in message
    assert "{'status': 'still running'}" in message


async def test_poll_until_async_accepts_success_with_state() -> None:
    assert await poll_until_async(lambda: (True, "ready")) is None
