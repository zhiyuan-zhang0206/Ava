import asyncio
from typing import cast

import pytest

from base.daemon.shutdown import cancel_and_drain


def _raise(error: BaseException) -> None:
    raise error


async def _interrupted_group(
    interruption: KeyboardInterrupt,
    events: list[str],
    cleanup_error: BaseException | None = None,
) -> None:
    async def stop() -> None:
        raise interruption

    try:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(stop())
            await asyncio.Event().wait()
    finally:
        events.append("cleanup-start")
        await asyncio.sleep(0.01)
        events.append("cleanup-end")
        if cleanup_error is not None:
            raise cleanup_error


@pytest.mark.parametrize("ordinary_failure", [False, True])
def test_same_taskgroup_interrupt_finishes_async_cleanup(ordinary_failure: bool) -> None:
    interruption = KeyboardInterrupt("original stop")
    failure = RuntimeError("cleanup failed") if ordinary_failure else None
    events: list[str] = []
    with asyncio.Runner() as runner:
        with pytest.raises(KeyboardInterrupt) as first:
            runner.run(_interrupted_group(interruption, events, failure))
        assert first.value is interruption
        try:
            failures = cancel_and_drain(runner, interrupted_by=first.value)
        except BaseException as error:
            pytest.fail(f"original stop escaped the drain: {error!r}")
        assert events == ["cleanup-start", "cleanup-end"]
        assert failures == ([failure] if failure is not None else [])
        assert not asyncio.all_tasks(runner.get_loop())


@pytest.mark.parametrize("explicit_other", [False, True])
def test_unidentified_taskgroup_interrupt_keeps_original_object(explicit_other: bool) -> None:
    interruption = KeyboardInterrupt("original stop")
    other = KeyboardInterrupt("original stop") if explicit_other else None
    events: list[str] = []
    with asyncio.Runner() as runner:
        with pytest.raises(KeyboardInterrupt) as first:
            runner.run(_interrupted_group(interruption, events))
        with pytest.raises(KeyboardInterrupt) as repeated:
            cancel_and_drain(runner, interrupted_by=other)
        assert repeated.value is first.value is interruption
        assert events == ["cleanup-start", "cleanup-end"]


@pytest.mark.parametrize("second", [KeyboardInterrupt("second stop"), SystemExit(7)])
def test_new_control_event_during_cleanup_propagates(second: BaseException) -> None:
    interruption = KeyboardInterrupt("original stop")
    events: list[str] = []
    with asyncio.Runner() as runner:
        with pytest.raises(KeyboardInterrupt):
            runner.run(_interrupted_group(interruption, events, second))
        with pytest.raises(type(second)) as caught:
            cancel_and_drain(runner, interrupted_by=interruption)
        assert caught.value is second
        assert events == ["cleanup-start", "cleanup-end"]


def test_replay_does_not_cancel_async_cleanup_again_or_replace_its_result() -> None:
    interruption = KeyboardInterrupt("original stop")
    failure = RuntimeError("cleanup result retained")
    counts: list[int] = []
    events: list[str] = []

    async def run() -> None:
        loop = asyncio.get_running_loop()
        loop.call_soon(_raise, interruption)
        try:
            await asyncio.Event().wait()
        finally:
            current = asyncio.current_task()
            assert current is not None
            counts.append(current.cancelling())
            events.append("cleanup-start")
            loop.call_soon(_raise, interruption)
            await asyncio.sleep(0.01)
            counts.append(current.cancelling())
            events.append("cleanup-end")
            raise failure

    with asyncio.Runner() as runner:
        with pytest.raises(KeyboardInterrupt):
            runner.run(run())
        failures = cancel_and_drain(runner, interrupted_by=interruption)
        assert counts == [1, 1]
        assert events == ["cleanup-start", "cleanup-end"]
        assert len(failures) == 1 and failures[0] is failure


class _BrokenRunner:
    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    def get_loop(self) -> asyncio.AbstractEventLoop:
        raise self.failure


@pytest.mark.parametrize("failure", [RuntimeError("loop defect"), BaseException("unknown")])
def test_loop_access_error_is_not_a_replayed_stop(failure: BaseException) -> None:
    with pytest.raises(type(failure)) as caught:
        runner = cast(asyncio.Runner, _BrokenRunner(failure))
        cancel_and_drain(runner, interrupted_by=KeyboardInterrupt("stop"))
    assert caught.value is failure
