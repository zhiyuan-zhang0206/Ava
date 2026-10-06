"""Bounded best-effort stops of root's direct service children.

A live captured leader identifies its process group at signal time. Once the
leader exits, root does not enumerate descendants or reuse its old group number.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass

from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.ipc import UnitState
from services.supervision.ava_root.unit_records import _Generation, _UnitRuntime


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """Timing policy for the supervisor (test-tunable)."""

    stop_timeout_s: float = 10.0
    """Default TERM window; only explicit force permits a later KILL."""


class StoppingMixin:
    """Stop known direct children, without durable custody or descendant discovery."""

    _config: SupervisorConfig

    async def _stop_unit(self, runtime: _UnitRuntime, *, force: bool = False) -> None:
        generation = runtime.generation
        if generation is None:
            runtime.state = UnitState.STOPPED
            return
        await self._stop_generation(runtime, generation, force=force)

    async def _stop_generation(
        self, runtime: _UnitRuntime, generation: _Generation, *, force: bool = False
    ) -> None:
        """Signal a birth-validated live group and await the direct child's exit."""
        generation.closing = True
        identity = generation.identity
        if identity is not None:
            self._signal_owned(identity, force=False)
        window = self._stop_window(runtime)
        if not await self._wait_exit(generation, window):
            if force and identity is not None:
                self._signal_owned(identity, force=True)
                if await self._wait_exit(generation, window):
                    return
            raise RuntimeError(
                f"unit {runtime.manifest.id} did not stop within its {window:g}s window "
                f"(pid {generation.proc.pid})"
            )

    @staticmethod
    async def _wait_exit(generation: _Generation, timeout_s: float) -> bool:
        """Await the existing watcher, without cancelling it on timeout."""
        try:
            await asyncio.wait_for(generation.exited.wait(), timeout_s)
        except TimeoutError:
            return generation.exited.is_set()
        return True

    def _stop_window(self, runtime: _UnitRuntime) -> float:
        declared = runtime.manifest.stop_timeout_s
        return self._config.stop_timeout_s if declared is None else declared

    @staticmethod
    def _signal_owned(identity: OwnedProcess, *, force: bool) -> None:
        """Attempt the captured leader's group only while that leader still lives."""
        if not identity.live():
            return
        signum = signal.SIGKILL if force else signal.SIGTERM
        try:
            group = os.getpgid(identity.pid)
            if not identity.live():
                return
            if group == identity.pid and group != os.getpgrp():
                os.killpg(group, signum)
            else:
                identity.send_signal(signum)
        except ProcessLookupError:
            return
