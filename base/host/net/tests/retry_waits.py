"""The `retry_waits` fixture: observe the backoff waits of `base.host.net.resilience`.

`retry()` and `aretry()` sleep through the module's two public wait hooks,
`retry_sleep` and `retry_asleep`. A test that takes `retry_waits` gets both replaced
by recorders: nothing really sleeps, and the fixture's value is the list of waits
the retry loops asked for, sync and async in call order.

It is opt-in, never autouse: a test states which retry waits it observes, and can
assert the exact sequence.

Not a global no-op wait (issue #1001): a no-op sleep under a loop bounded by wall
clock spins until memory runs away. The hooks are read only by `retry()` and
`aretry()`, whose loops end after `Policy.max_attempts`; the recorder stops a caller
that loops around them anyway, by failing once `_MAX_WAITS` waits are recorded
instead of growing the list without bound.

Jitter is `jittered()`'s concern, not the hooks': a test that asserts exact waits
passes `jitter="none"` in its `Policy`, or patches `jittered` in the module that
imported it (`base.lm.call.jittered` for the model-call retry).
"""

import pytest

_MAX_WAITS = 1000


@pytest.fixture
def retry_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the retry wait hooks with recorders and return the recorded waits.

        def test_backs_off(retry_waits):
            call_that_retries_twice()
            assert retry_waits == [1.0, 2.0]

    The async hook is a real coroutine function, because `aretry()` awaits it. The
    hooks are restored when the test ends.
    """
    from base.host.net import resilience

    waits: list[float] = []

    def record(seconds: float) -> None:
        if len(waits) >= _MAX_WAITS:
            raise AssertionError(
                f"retry_waits recorded {_MAX_WAITS} waits: a loop keeps retrying because "
                "no wait takes time (issue #1001), so it will not end by itself"
            )
        waits.append(seconds)

    async def arecord(seconds: float) -> None:
        record(seconds)

    monkeypatch.setattr(resilience, "retry_sleep", record)
    monkeypatch.setattr(resilience, "retry_asleep", arecord)
    return waits
