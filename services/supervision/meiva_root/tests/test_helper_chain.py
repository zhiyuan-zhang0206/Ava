from __future__ import annotations

import asyncio

import pytest

from services.supervision.meiva_root.helper_chain import HelperChainConfig, HelperChainWatch


def test_config_rejects_a_nonpositive_interval() -> None:
    for interval in (0.0, -1.0, float("inf")):
        with pytest.raises(ValueError):
            HelperChainConfig(interval_s=interval)


def test_config_rejects_an_empty_confirmation() -> None:
    with pytest.raises(ValueError):
        HelperChainConfig(confirm_rounds=0)


def test_retires_on_the_confirming_broken_reading() -> None:
    readings: list[int] = []
    retired: list[str] = []

    def intact() -> bool:
        readings.append(len(readings))
        return False

    async def scenario() -> None:
        watch = HelperChainWatch(
            retire=retired.append,
            stop=asyncio.Event(),
            config=HelperChainConfig(interval_s=0.001, confirm_rounds=3),
            intact=intact,
        )
        await watch.run()

    asyncio.run(scenario())
    assert len(readings) == 3
    assert len(retired) == 1
    assert "3 consecutive broken readings" in retired[0]


def test_a_single_broken_reading_does_not_retire() -> None:
    readings = iter([True, False, True, False, True, False])

    async def scenario() -> list[str]:
        retired: list[str] = []

        def intact() -> bool:
            return next(readings, True)

        stop = asyncio.Event()
        watch = HelperChainWatch(
            retire=retired.append,
            stop=stop,
            config=HelperChainConfig(interval_s=0.001, confirm_rounds=2),
            intact=intact,
        )
        task = asyncio.create_task(watch.run())
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        return retired

    assert asyncio.run(scenario()) == []


def test_an_intact_chain_never_retires() -> None:
    async def scenario() -> list[str]:
        retired: list[str] = []
        stop = asyncio.Event()
        watch = HelperChainWatch(
            retire=retired.append,
            stop=stop,
            config=HelperChainConfig(interval_s=0.001, confirm_rounds=1),
            intact=lambda: True,
        )
        task = asyncio.create_task(watch.run())
        await asyncio.sleep(0.02)
        assert not task.done()
        stop.set()
        await asyncio.wait_for(task, timeout=1)
        return retired

    assert asyncio.run(scenario()) == []


def _no_retirement(_detail: str) -> None:
    pass


def test_the_evidence_line_names_the_root_and_the_expected_helper() -> None:
    watch = HelperChainWatch(retire=_no_retirement, stop=asyncio.Event(), marker=lambda: "4242")
    detail = watch._detail(2)
    assert "marker pid 4242" in detail
    assert "2 consecutive broken readings" in detail


def test_the_evidence_line_reads_an_absent_marker() -> None:
    watch = HelperChainWatch(retire=_no_retirement, stop=asyncio.Event(), marker=lambda: None)
    assert "marker pid absent" in watch._detail(1)
