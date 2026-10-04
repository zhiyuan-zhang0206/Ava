"""`send_with_retry`'s log contract (2026-10-03 triage, E3).

The first failed attempt is the transient norm (the measured ~0.65s connect
window; a flaky link): WARNING with a one-line cause, no traceback. Only when
the single retry fails does the send log ERROR + stack and run the push-failure
watchdog. This module exists beside `test_im_bridge_core.py` because that file
sits at its frozen size ceiling.
"""

from __future__ import annotations

import logging

import pytest

from services.im_bridge import push_watchdog
from services.im_bridge.tests.slices import im_bridge_config
from services.im_bridge.types import Reply

_LOGGER = "services.im_bridge.core.push_watchdog"


class _Adapter:
    channel = "weixin"
    push_failures = 0

    def __init__(self, *, fail_attempts: int) -> None:
        self.attempts = 0
        self._fail_attempts = fail_attempts

    async def send(
        self,
        _chat_id: str,
        _text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        del buttons, markdown
        self.attempts += 1
        if self.attempts <= self._fail_attempts:
            raise RuntimeError("iLink sendmessage error: ret=-2 errmsg=prepare failed")


class _Core:
    """The slice `send_with_retry`/`alert_push_failure` read."""

    config = im_bridge_config(im_push_retry_backoff_seconds=0.0, im_push_retry_jitter_seconds=0.0)


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(push_watchdog, "_sleep", _sleep)


async def test_first_failure_logs_warning_and_a_healed_retry_stays_clean(
    _no_sleep: None, caplog: pytest.LogCaptureFixture
) -> None:
    """One failed attempt = WARNING, no traceback; the retry heals the send."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    adapter = _Adapter(fail_attempts=1)

    await push_watchdog.send_with_retry(_Core(), "weixin", "chat", Reply("hello"), adapter)

    assert adapter.attempts == 2  # exactly one retry
    lines = [r for r in caplog.records if r.getMessage().startswith("send failed")]
    assert [(r.levelname, r.exc_info) for r in lines] == [("WARNING", None)]
    assert not [r for r in caplog.records if "send retry failed" in r.getMessage()]


async def test_failed_retry_keeps_error_with_traceback(
    _no_sleep: None, caplog: pytest.LogCaptureFixture
) -> None:
    """Only a retry failure escalates: ERROR + traceback (the push-failure
    watchdog then decides about the cross-channel alert)."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    adapter = _Adapter(fail_attempts=2)

    await push_watchdog.send_with_retry(_Core(), "weixin", "chat", Reply("hello"), adapter)

    assert adapter.attempts == 2
    retries = [r for r in caplog.records if "send retry failed" in r.getMessage()]
    assert [(r.levelname, r.exc_info is not None) for r in retries] == [("ERROR", True)]
