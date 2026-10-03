"""`scripts/content_lint/lint_quiesced_loops.py` — every resident periodic loop declares its
stance on the stop window: it gates on `admission.quiesced()` or carries a reasoned exemption."""

from __future__ import annotations

import importlib
import textwrap

import pytest

_lint = importlib.import_module("scripts.content_lint.lint_quiesced_loops")


def _violations(src: str) -> list[tuple[int, str]]:
    return _lint.violations_in_source(textwrap.dedent(src))


def _lines(src: str) -> list[int]:
    return [line for line, _reason in _violations(src)]


def test_a_resident_loop_without_a_gate_or_exemption_is_flagged() -> None:
    src = """
        import asyncio

        async def poll(pool):
            while True:
                await asyncio.sleep(1)
                with pool.connection():
                    pass
    """
    assert _lines(src) == [5]


@pytest.mark.parametrize("gate", ["admission.quiesced()", "admission.in_stop_leg()"])
def test_a_function_that_consults_the_stop_window_is_gated(gate: str) -> None:
    src = f"""
        import asyncio

        async def poll(pool):
            while True:
                await asyncio.sleep(1)
                if {gate}:
                    continue
    """
    assert _violations(src) == []


def test_the_gate_may_be_imported_by_name() -> None:
    src = """
        import asyncio
        from base.deploy.maintenance.admission import quiesced

        async def poll():
            while True:
                await asyncio.sleep(1)
                if quiesced():
                    continue
    """
    assert _violations(src) == []


def test_an_event_set_loop_is_a_resident_shape() -> None:
    src = """
        import asyncio

        async def poll(stop):
            while not stop.is_set():
                await asyncio.sleep(1)
    """
    assert _lines(src) == [5]


def test_a_wait_with_a_timeout_is_a_periodic_wait() -> None:
    src = """
        import asyncio

        async def poll(stop):
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass
    """
    assert _lines(src) == [5]


@pytest.mark.parametrize(
    "src",
    [
        # a bounded retry: the test is a deadline, not `True`
        "async def f(deadline):\n    while now() < deadline:\n        await asyncio.sleep(1)\n",
        # no periodic wait: it blocks on a queue
        "async def f(queue):\n    while True:\n        await queue.get()\n",
        # the sleep belongs to a nested function, not the loop
        "def f():\n    while True:\n        def later():\n            time.sleep(1)\n        later()\n",
    ],
)
def test_loops_that_are_not_resident_periodic_loops_are_not_flagged(src: str) -> None:
    assert _violations(src) == []


def test_a_nested_loop_is_covered_by_its_outer_loop() -> None:
    src = """
        import asyncio

        async def run():
            while True:
                while True:
                    await asyncio.sleep(1)
    """
    assert _lines(src) == [5]


@pytest.mark.parametrize(
    "layout",
    [
        "{loop}  # quiesce-exempt: no database",
        "# quiesce-exempt: no database\n{loop}",
    ],
)
def test_a_reasoned_marker_on_or_above_the_loop_exempts_it(layout: str) -> None:
    loop = "while True:\n    await asyncio.sleep(1)"
    first, _, rest = loop.partition("\n")
    body = layout.format(loop=first) + "\n" + rest
    src = "async def f():\n" + textwrap.indent(body, "    ") + "\n"
    assert _violations(src) == []


def test_a_marker_without_a_reason_does_not_exempt() -> None:
    src = """
        async def f():
            # quiesce-exempt:
            while True:
                await asyncio.sleep(1)
    """
    assert _lines(src) == [4]


def test_a_marker_that_no_loop_starts_below_is_stale() -> None:
    src = """
        # quiesce-exempt: no database

        x = 1
    """
    assert _lines(src) == [2]


def test_a_marker_inside_a_string_is_not_a_marker() -> None:
    src = """
        async def f():
            note = "# quiesce-exempt: no database"
            while True:
                await asyncio.sleep(1)
    """
    assert _lines(src) == [4]


def test_a_gate_in_another_function_does_not_cover_the_loop() -> None:
    src = """
        import asyncio

        def held():
            return admission.quiesced()

        async def poll():
            while True:
                await asyncio.sleep(1)
    """
    assert _lines(src) == [8]
