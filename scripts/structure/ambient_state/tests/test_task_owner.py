"""Necessary Task ownership wiring, and independent lifecycle counterexamples."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

from scripts.structure import ambient_state

SOURCE = """
import asyncio

class Service:
    def __init__(self, report):
        self._operations = {}
        self._abandoned = set()
        self._errors = []
        self._report = report
        self._stopped = False

    def start(self, work):
        if self._stopped:
            raise RuntimeError('stopped')
        task = asyncio.create_task(work())
        self._register(task)
        return task

    def _register(self, task):
        self._operations[task] = True
        task.add_done_callback(self._completed)

    def _completed(self, task):
        if task.cancelled():
            self._operations.pop(task)
            return
        error = task.exception()
        if task not in self._abandoned:
            return
        self._operations.pop(task)
        self._abandoned.remove(task)
        if error is not None:
            self._failed(error, task)

    def _failed(self, error, task):
        self._errors.append(error)
        self._report(error, task)

    def abandon(self, task):
        self._abandoned.add(task)
        if task.done():
            self._completed(task)
        elif not task.cancelling():
            task.cancel()

    def claim(self, task):
        try:
            return task.result()
        finally:
            self._operations.pop(task)

    def _cancel(self):
        for task in self._operations:
            if not task.done() and task not in self._abandoned:
                self._abandoned.add(task)
                task.cancel()

    @property
    def unfinished(self):
        return tuple(task.get_name() for task in self._operations if not task.done())

    def _check_errors(self):
        if self._errors:
            first, *additional = self._errors
            raise first

    async def stop(self, timeout=1):
        self._stopped = True
        self._cancel()
        pending = {task for task in self._operations if not task.done()}
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        self._check_errors()
        return self.unfinished
"""


def _sites(source: str, root: Path) -> dict[str, list[int]]:
    return ambient_state.measure(ast.parse(source), "base/service.py", root)


def test_visible_task_owner_wiring_is_supported(tmp_path: Path) -> None:
    assert _sites(SOURCE, tmp_path) == {}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("self._register(task)", "pass"),
        ("self._register(task)", "self._register(other)"),
        ("self._register(task)", "await pause()\n        self._register(task)"),
        ("self._operations[task] = True", "self._current = task"),
        ("self._operations[task] = True", "self._operations[other] = True"),
        ("self._operations = {}", "self._operations = pretend_registry()"),
        ("self._operations = {}", "self._operations = []"),
        (
            "self._operations = {}",
            "self._operations = {}\n        self._operations = pretend_registry()",
        ),
        ("task.add_done_callback(self._completed)", "other.add_done_callback(self._completed)"),
        ("task.add_done_callback(self._completed)", "pass"),
        ("error = task.exception()", "error = other.exception()"),
        ("if task.cancelled():", "if other.cancelled():"),
        ("error = task.exception()", "task = other\n        error = task.exception()"),
        (
            "error = task.exception()",
            "error = task.exception()\n        error = RuntimeError('different')",
        ),
        ("error = task.exception()", "error = RuntimeError('different')"),
        ("self._errors.append(error)", "self._errors.append(RuntimeError('different'))"),
        ("self._errors = []", "self._errors = pretend_receipt()"),
        (
            "self._errors.append(error)",
            "error = RuntimeError('different')\n        self._errors.append(error)",
        ),
        ("self._report(error, task)", "pass"),
        ("self._errors.append(error)", "return\n        self._errors.append(error)"),
        ("def _completed(self, task):", "def _completed(self, task):\n        return"),
        ("def _cancel(self):", "def _cancel(self):\n        return"),
        ("self._report(error, task)", "str(error)"),
        ("raise first", "raise RuntimeError('different')"),
        ("raise first", "first = RuntimeError('different')\n            raise first"),
        ("raise first", "return"),
        ("await asyncio.wait(pending, timeout=timeout)", "asyncio.wait(pending, timeout=timeout)"),
        ("await asyncio.wait(pending, timeout=timeout)", "await asyncio.wait(pending)"),
        (
            "await asyncio.wait(pending, timeout=timeout)",
            "await asyncio.wait(pending, timeout=None)",
        ),
        (
            "await asyncio.wait(pending, timeout=timeout)",
            "await asyncio.wait(other, timeout=timeout)",
        ),
        ("self._check_errors()", "pass"),
        ("return self.unfinished", "return ()"),
        ("self._cancel()", "pass"),
        ("task.cancel()", "other.cancel()"),
        ("for task in self._operations:", "for task in self._other:"),
        ("self._errors.append(error)", "self._errors.append(error)\n        self._errors.clear()"),
        ("self._register(task)", "if False:\n            self._register(task)"),
        ("def _register(self, task):", "async def _register(self, task):"),
        (
            "def _register(self, task):",
            "def _register(self, task):\n        raise RuntimeError('unregistered')",
        ),
        ("def _completed(self, task):", "async def _completed(self, task):"),
        ("def _cancel(self):", "async def _cancel(self):"),
        ("def _check_errors(self):", "async def _check_errors(self):"),
        ("self._operations[task] = True", "task = other\n        self._operations[task] = True"),
        (
            "self._operations[task] = True",
            "self._operations[task] = True\n        self._operations.pop(task)",
        ),
        (
            "self._operations[task] = True",
            "if skip:\n            return\n        self._operations[task] = True",
        ),
    ],
)
def test_missing_or_decorative_wiring_remains_a_finding(
    tmp_path: Path, before: str, after: str
) -> None:
    source = SOURCE.replace(before, after)
    assert list(_sites(source, tmp_path)) == ["base/service.py::asyncio-task:Service.start"]


def test_names_and_import_aliases_do_not_grant_or_remove_ownership(tmp_path: Path) -> None:
    renamed = SOURCE.replace("import asyncio", "import asyncio as scheduler").replace(
        "asyncio.", "scheduler."
    )
    for old, new in (("_register", "retain"), ("_operations", "jobs"), ("_completed", "harvest")):
        renamed = renamed.replace(old, new)
    assert _sites(renamed, tmp_path) == {}


def test_uninvoked_nested_callback_receipt_does_not_count(tmp_path: Path) -> None:
    source = SOURCE.replace(
        "        self._errors.append(error)\n        self._report(error, task)",
        "        def pretend():\n            self._errors.append(error)\n            self._report(error, task)",
    )
    assert _sites(source, tmp_path)


def test_renaming_spawn_behind_a_helper_does_not_retire_it(tmp_path: Path) -> None:
    source = SOURCE.replace(
        "        task = asyncio.create_task(work())\n        self._register(task)",
        "        task = self._spawn(work)",
    )
    source += "\n    def _spawn(self, work):\n        return asyncio.create_task(work())\n"
    assert list(_sites(source, tmp_path)) == ["base/service.py::asyncio-task:Service._spawn"]


def test_real_set_registry_and_original_field_receipt_are_supported(tmp_path: Path) -> None:
    source = (
        SOURCE.replace("self._operations = {}", "self._operations = set()")
        .replace("self._operations[task] = True", "self._operations.add(task)")
        .replace("self._operations.pop(task)", "self._operations.remove(task)")
    )
    source = (
        source.replace("self._errors = []", "self._errors = None")
        .replace("self._errors.append(error)", "self._errors = error")
        .replace("first, *additional = self._errors\n            raise first", "raise self._errors")
    )
    assert _sites(source, tmp_path) == {}


def test_uninvoked_method_reference_does_not_count_as_teardown_edge(tmp_path: Path) -> None:
    assert _sites(SOURCE.replace("self._cancel()", "unused = self._cancel"), tmp_path)


def test_actual_task_identity_and_direct_registry_snapshot_are_supported(tmp_path: Path) -> None:
    source = SOURCE.replace(
        "await asyncio.wait(pending, timeout=timeout)",
        "await asyncio.wait(set(self._operations), timeout=timeout)",
    )
    source = source.replace("task.get_name() for task", "task for task").replace(
        "raise first", "raise self._errors[0]"
    )
    assert _sites(source, tmp_path) == {}


def test_unfinished_identities_must_come_from_the_waited_registry(tmp_path: Path) -> None:
    assert _sites(
        SOURCE.replace(
            "task.get_name() for task in self._operations",
            "task.get_name() for task in self._other",
        ),
        tmp_path,
    )


def test_completed_tasks_cannot_be_called_unfinished(tmp_path: Path) -> None:
    assert _sites(
        SOURCE.replace(
            "task.get_name() for task in self._operations if not task.done()",
            "task.get_name() for task in self._operations if task.done()",
        ),
        tmp_path,
    )


class _Service(Protocol):
    _operations: dict[asyncio.Task[Any], bool]
    _errors: list[BaseException]

    def start(self, work: Callable[[], Coroutine[Any, Any, None]]) -> asyncio.Task[None]: ...
    def claim(self, task: asyncio.Task[None]) -> None: ...
    async def stop(self, timeout: float = 1) -> tuple[str, ...]: ...


def _service(source: str, report: Callable[[BaseException, asyncio.Task[None]], None]) -> _Service:
    namespace: dict[str, Any] = {}
    exec(compile(source, "owned-task-example.py", "exec"), namespace)
    return cast(_Service, namespace["Service"](report))


@pytest.mark.asyncio
async def test_active_original_error_is_received_by_caller_without_late_replay() -> None:
    original = RuntimeError("active bug")
    reports: list[BaseException] = []
    service = _service(SOURCE, lambda error, _task: reports.append(error))

    async def work() -> None:
        raise original

    task = service.start(work)
    with pytest.raises(RuntimeError) as caught:
        await task
    assert caught.value is original
    assert task in service._operations
    with pytest.raises(RuntimeError) as claimed:
        service.claim(task)
    assert claimed.value is original
    assert await service.stop() == ()
    assert reports == []


@pytest.mark.asyncio
async def test_bounded_stop_retains_resistant_task_and_receives_late_original_error() -> None:
    entered, release, reported = (asyncio.Event() for _ in range(3))
    original = RuntimeError("late bug")
    reports: list[BaseException] = []

    def report(error: BaseException, task: asyncio.Task[None]) -> None:
        reports.append(error)
        reported.set()

    service = _service(SOURCE, report)

    async def work() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise original from None

    task = service.start(work)
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        unfinished = await asyncio.wait_for(service.stop(timeout=0.01), timeout=1)
        assert unfinished == (task.get_name(),)
        assert task in service._operations and not task.done()
        release.set()
        await asyncio.wait_for(reported.wait(), timeout=1)
        assert reports == [original] and service._errors == [original]
        with pytest.raises(RuntimeError) as caught:
            await service.stop()
        assert caught.value is original
        assert task not in service._operations
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_static_wiring_does_not_prove_admission_after_stop(tmp_path: Path) -> None:
    source = SOURCE.replace(
        "        if self._stopped:\n            raise RuntimeError('stopped')\n", ""
    )
    assert _sites(source, tmp_path) == {}
    service = _service(source, lambda error, _task: pytest.fail(str(error)))
    assert await service.stop() == ()
    entered = asyncio.Event()

    async def work() -> None:
        entered.set()
        await asyncio.Event().wait()

    task = service.start(work)
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert task in service._operations and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_fake_append_receipt_loses_real_late_error_and_is_rejected(tmp_path: Path) -> None:
    source = (
        SOURCE.replace("self._errors = []", "self._errors = DiscardReceipt()")
        + """
class DiscardReceipt:
    def append(self, error):
        pass
    def __bool__(self):
        return False
"""
    )
    assert _sites(source, tmp_path)
    entered, release, reported = (asyncio.Event() for _ in range(3))
    original = RuntimeError("lost receipt")
    reports: list[BaseException] = []

    def report(error: BaseException, _task: asyncio.Task[None]) -> None:
        reports.append(error)
        reported.set()

    service = _service(source, report)

    async def work() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise original from None

    task = service.start(work)
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert await asyncio.wait_for(service.stop(timeout=0.01), timeout=1)
        release.set()
        await asyncio.wait_for(reported.wait(), timeout=1)
        assert reports == [original]
        assert await service.stop() == ()  # The decorative sink erased the stop receipt.
        with pytest.raises(RuntimeError) as caught:
            task.result()
        assert caught.value is original
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
