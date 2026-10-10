"""Owned-thread structural evidence, counterexamples and real lifecycle receipts."""

from __future__ import annotations

import ast
import pathlib
import threading
from collections.abc import Callable
from typing import Protocol, cast

import pytest

from scripts.structure import ambient_state

SOURCE = """
import threading

class Service:
    def __init__(self, work, report):
        self._work = work
        self._report = report
        self._stop_requested = threading.Event()
        self._finished = threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._work(self._stop_requested)
        except BaseException as error:
            self._error = error
            self._report(error)
        finally:
            self._finished.set()

    def stop(self, timeout=1):
        self._stop_requested.set()
        self._thread.join(timeout=timeout)
        unfinished = self._thread.is_alive()
        if self._error is not None:
            raise self._error
        return not unfinished
"""


def _sites(source: str, root: pathlib.Path) -> dict[str, list[int]]:
    return ambient_state.measure(ast.parse(source), "base/service.py", root)


def test_visible_lifecycle_is_accepted(tmp_path: pathlib.Path) -> None:
    assert _sites(SOURCE, tmp_path) == {}


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("self._thread.join(timeout=timeout)", "pass"),
        ("self._thread.join(timeout=timeout)", "self._other.join(timeout=timeout)"),
        ("self._thread.join(timeout=timeout)", "self._thread.join()"),
        ("self._thread.join(timeout=timeout)", "self._thread.join(timeout=None)"),
        ("self._thread.join(timeout=timeout)", "self._thread.join(timeout=float('inf'))"),
        ("self._thread.is_alive()", "False"),
        ("except BaseException as error:", "except Exception as error:"),
        ("self._error = error", "self._error = None"),
        ("self._report(error)", "self._report(error)\n            self._error = None"),
        (
            "self._thread.join(timeout=timeout)",
            "if False:\n            self._thread.join(timeout=timeout)",
        ),
        ("self._report(error)", "pass"),
        ("self._report(error)", "str(error)"),
        ("raise self._error", "return False"),
        ("self._stop_requested.set()", "self._finished.set()"),
        ("self._thread.start()", "pass"),
        ("self._thread = threading.Thread", "worker = threading.Thread"),
        ("target=self._run", "target=lambda: self._work(self._stop_requested)"),
    ],
)
def test_partial_or_decorative_ownership_remains_a_finding(
    tmp_path: pathlib.Path,
    before: str,
    after: str,
) -> None:
    assert list(_sites(SOURCE.replace(before, after), tmp_path)) == [
        "base/service.py::thread:Service.__init__",
    ]


def test_catch_must_cover_the_whole_worker_entry(tmp_path: pathlib.Path) -> None:
    source = SOURCE.replace("    def _run(self):\n", "    def _run(self):\n        unobserved()\n")
    assert _sites(source, tmp_path)


def test_nested_uninvoked_cleanup_does_not_count(tmp_path: pathlib.Path) -> None:
    source = SOURCE.replace(
        "        self._thread.join(timeout=timeout)",
        "        def pretend():\n            self._thread.join(timeout=timeout)",
    )
    assert _sites(source, tmp_path)


def test_actual_helper_edges_and_local_handle_error_aliases_are_followed(
    tmp_path: pathlib.Path,
) -> None:
    source = SOURCE.replace(
        "        self._thread.join(timeout=timeout)",
        "        self._join(timeout)",
    ).replace("raise self._error", "error = self._error\n            raise error")
    source += """
    def _join(self, timeout):
        worker = self._thread
        worker.join(timeout=timeout)
"""
    assert _sites(source, tmp_path) == {}
    assert _sites(source.replace("self._join(timeout)", "pass"), tmp_path)


class _Service(Protocol):
    _thread: threading.Thread

    def stop(self, timeout: float = 1) -> bool: ...

    def start(self) -> None: ...


Factory = Callable[[Callable[[threading.Event], None], Callable[[BaseException], None]], _Service]


def _service_class() -> Factory:
    namespace: dict[str, object] = {}
    exec(compile(SOURCE, "owned-thread-example.py", "exec"), namespace)
    return cast(Factory, namespace["Service"])


def test_real_owner_stops_and_observes_normal_completion() -> None:
    service_class = _service_class()
    admitted = threading.Event()

    def work(stop: threading.Event) -> None:
        admitted.set()
        assert stop.wait(1)

    service = service_class(work, lambda error: pytest.fail(str(error)))
    assert admitted.wait(1)
    assert service.stop()
    assert not service._thread.is_alive()
    assert service.stop()


def test_unknown_late_failure_is_visible_then_raised_by_original_owner() -> None:
    service_class = _service_class()
    visible = threading.Event()
    failure = SystemExit("worker bug")
    reported: list[BaseException] = []

    def report(error: BaseException) -> None:
        reported.append(error)
        visible.set()

    def fail(stop: threading.Event) -> None:
        raise failure

    service = service_class(fail, report)
    assert visible.wait(1)
    assert reported == [failure]
    with pytest.raises(SystemExit) as caught:
        service.stop()
    assert caught.value is failure
    assert not service._thread.is_alive()


def test_structural_pass_does_not_claim_native_work_was_interrupted(
    tmp_path: pathlib.Path,
) -> None:
    """A callback can ignore the signal: finite join must preserve residual truth."""
    assert _sites(SOURCE, tmp_path) == {}
    service_class = _service_class()
    blocked = threading.Event()
    release = threading.Event()

    def work(stop: threading.Event) -> None:
        blocked.set()
        release.wait(2)

    service = service_class(work, lambda error: pytest.fail(str(error)))
    try:
        assert blocked.wait(1)
        assert service.stop(timeout=0.01) is False
        assert service._thread.is_alive()
    finally:
        release.set()
        assert service.stop()


def test_restart_requires_visible_stop_admission_fence(tmp_path: pathlib.Path) -> None:
    spawn = "        self._thread = threading.Thread(target=self._run, daemon=True)\n        self._thread.start()"
    source = SOURCE.replace(spawn, "        self.start()")
    source += "\n    def start(self):\n" + spawn + "\n"
    assert _sites(source, tmp_path)
    guarded = source.replace(
        spawn,
        "        if self._stop_requested.is_set():\n            raise RuntimeError('closed')\n"
        + spawn,
    )
    assert _sites(guarded, tmp_path)
    namespace: dict[str, object] = {}
    exec(compile(guarded, "restart-example.py", "exec"), namespace)
    service_class = cast(Factory, namespace["Service"])

    def work(stop: threading.Event) -> None:
        stop.wait(1)

    service = service_class(work, lambda error: pytest.fail(str(error)))
    assert service.stop()
    # A stop-only fence refuses restart after stop, but cannot fence a second
    # start while the first is live. This source is intentionally rejected.
    start = service.start
    with pytest.raises(RuntimeError, match="closed"):
        start()


def test_alias_imports_and_delegated_worker_body_are_visible(tmp_path: pathlib.Path) -> None:
    source = SOURCE.replace("import threading", "import threading as th").replace(
        "threading.", "th."
    )
    source = source.replace(
        "            self._work(self._stop_requested)", "            self._body()"
    )
    source += "\n    def _body(self):\n        self._work(self._stop_requested)\n"
    assert _sites(source, tmp_path) == {}


@pytest.mark.parametrize(
    "tail",
    ["raise RuntimeError('cleanup bug')", "self._error = None", "self._work(self._stop_requested)"],
)
def test_unprotected_finally_work_or_receipt_overwrite_is_rejected(
    tmp_path: pathlib.Path, tail: str
) -> None:
    source = SOURCE.replace("            self._finished.set()", "            " + tail)
    assert _sites(source, tmp_path)


def test_unprotected_else_work_is_rejected(tmp_path: pathlib.Path) -> None:
    source = SOURCE.replace(
        "        finally:",
        "        else:\n            self._work(self._stop_requested)\n        finally:",
    )
    assert _sites(source, tmp_path)


def test_two_constructors_cannot_own_the_same_handle(tmp_path: pathlib.Path) -> None:
    spawn = "        self._thread = threading.Thread(target=self._run, daemon=True)"
    source = SOURCE.replace(spawn, spawn + "\n" + spawn)
    findings = _sites(source, tmp_path)
    assert len(findings["base/service.py::thread:Service.__init__"]) == 2


def test_finally_can_erase_an_actual_failure_so_its_source_is_rejected(
    tmp_path: pathlib.Path,
) -> None:
    source = SOURCE.replace(
        "            self._finished.set()",
        "            self._error = None\n            self._finished.set()",
    )
    assert _sites(source, tmp_path)
    namespace: dict[str, object] = {}
    exec(compile(source, "lost-error.py", "exec"), namespace)
    service_class = cast(Factory, namespace["Service"])
    failure = RuntimeError("original worker bug")
    reported: list[BaseException] = []

    def fail(stop: threading.Event) -> None:
        raise failure

    service = service_class(fail, reported.append)
    assert service.stop()
    assert reported == [failure]
    # The original error was observed, then erased instead of propagated.
    assert not service._thread.is_alive()


def test_stop_only_fence_can_overwrite_a_live_thread_and_is_rejected(
    tmp_path: pathlib.Path,
) -> None:
    spawn = "        self._thread = threading.Thread(target=self._run, daemon=True)\n        self._thread.start()"
    source = SOURCE.replace(spawn, "        self.start()")
    source += (
        "\n    def start(self):\n        if self._stop_requested.is_set():\n            raise RuntimeError('closed')\n"
        + spawn
        + "\n"
    )
    assert _sites(source, tmp_path)
    namespace: dict[str, object] = {}
    exec(compile(source, "overwritten-live-handle.py", "exec"), namespace)
    service_class = cast(Factory, namespace["Service"])
    admitted = [threading.Event(), threading.Event()]
    entries: list[threading.Thread] = []
    lock = threading.Lock()

    def work(stop: threading.Event) -> None:
        with lock:
            index = len(entries)
            entries.append(threading.current_thread())
            admitted[index].set()
        stop.wait(2)

    service = service_class(work, lambda error: pytest.fail(str(error)))
    original = service._thread
    try:
        assert admitted[0].wait(1)
        service.start()
        assert admitted[1].wait(1)
        assert service._thread is not original
        assert original.is_alive()
    finally:
        assert service.stop()
        original.join(timeout=1)
        assert not original.is_alive()
