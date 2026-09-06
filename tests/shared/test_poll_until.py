from __future__ import annotations

import pytest

from tests.shared.poll_until import poll_until


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
