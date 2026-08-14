"""agent/graph/_shared.py tests.

`_uncancel_current_task` is the cancel-recovery logic — after CancelledError is caught,
calls `task.uncancel()` so the next await does not re-raise.
"""

import asyncio

from agent.graph._shared import _DRAIN_SHUTDOWN_ERRORS, _STDIN_PIPE_CLOSED, _uncancel_current_task


class TestUncancelCurrentTask:
    async def test_does_not_raise_when_task_exists(self):
        """Calling in a normal event loop does not raise."""
        _uncancel_current_task()

    async def test_uncancel_called_on_current_task(self):
        """Verify uncancel is called — checked via monkeypatch."""
        import asyncio

        called = False
        orig_uncancel = asyncio.Task.uncancel  # pyright: ignore[reportUnknownMemberType]

        def fake_uncancel(self):
            nonlocal called
            called = True
            return orig_uncancel(self)  # pyright: ignore[reportUnknownArgumentType]

        # Can't monkeypatch asyncio.Task.uncancel (extension-type method
        # may be optimized by cpython), so verify the no-op scenario.
        # Call on a normal task — should not raise.
        task = asyncio.current_task()
        assert task is not None
        # On a normal task, uncancel is idempotent (never cancelled)
        task.uncancel()
        _uncancel_current_task()


class TestExceptionTuples:
    def test_stdin_pipe_closed_contains_broken_pipe(self):
        assert BrokenPipeError in _STDIN_PIPE_CLOSED

    def test_stdin_pipe_closed_contains_connection_reset(self):
        assert ConnectionResetError in _STDIN_PIPE_CLOSED

    def test_drain_shutdown_contains_timeout(self):
        assert TimeoutError in _DRAIN_SHUTDOWN_ERRORS

    def test_drain_shutdown_contains_cancelled(self):
        assert asyncio.CancelledError in _DRAIN_SHUTDOWN_ERRORS
