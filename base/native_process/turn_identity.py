"""The actual resources retained by one hosted turn's composition root."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from base.deploy.stop_timing import CANCEL_UNWIND_TIMEOUT_S
from base.log import logger


@dataclass
class HostedTurnResources:
    """Actual unresolved domains held by one turn Task, never by the model cache."""

    unresolved: dict[Path, object | None] = field(default_factory=dict[Path, object | None])
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    completions: set[asyncio.Task[None]] = field(default_factory=set[asyncio.Task[None]])
    service: HostedServiceResources | None = None

    def require_service(self) -> HostedServiceResources:
        """Late work needs the actual service lifespan, beyond this turn's return."""
        if self.service is None:
            raise RuntimeError("hosted asynchronous resources require their service owner")
        return self.service

    def complete(self, request: Path, expected: object | None) -> bool:
        """Only the original resource owner may discharge its exact entry."""
        if request not in self.unresolved or self.unresolved[request] is not expected:
            return False
        del self.unresolved[request]
        self.changed.set()
        return True


class HostedServiceResources:
    """The host's cross-turn completion span, joined before its database pools close.

    A late failure is retained and reported immediately without cancelling other
    turns. Only service stop/join raises it. Its original resource scope stays
    alive with the failure; observing an error never certifies resource cleanup.
    """

    def __init__(self) -> None:
        self._tasks = asyncio.TaskGroup()
        self._started = False
        self._closed = False
        self._joined = False
        self._pending: set[asyncio.Task[None]] = set()
        self._turns: set[asyncio.Task[Any]] = set()
        self.failures: list[tuple[HostedTurnResources, BaseException]] = []

    async def turn(self) -> HostedTurnResources:
        if self._closed:
            raise RuntimeError("hosted resource service is already closed")
        if not self._started:
            await self._tasks.__aenter__()
            self._started = True
        return HostedTurnResources(service=self)

    @contextmanager
    def hold_turn(self, task: asyncio.Task[Any] | None) -> Generator[None]:
        """Pool closure also waits for the actual roots that can create more resource work."""
        if task is None:
            raise RuntimeError("a hosted turn requires its actual task owner")
        self._turns.add(task)
        try:
            yield
        finally:
            self._turns.discard(task)

    def watch(self, operation: Coroutine[Any, Any, None], *, name: str) -> asyncio.Task[None]:
        """Start a subscription whose invocation consumes its observed result."""
        task = self._tasks.create_task(operation, name=name)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    def record_failure(
        self, scope: HostedTurnResources, error: BaseException, *, name: str
    ) -> None:
        if any(prior is scope and previous is error for prior, previous in self.failures):
            return
        self.failures.append((scope, error))
        logger.opt(exception=error).error(
            "late hosted resource failure retained until service stop/join: {name}; "
            "unresolved requests: {requests}",
            name=name,
            requests=[str(request) for request in scope.unresolved],
        )

    def complete_later(
        self, scope: HostedTurnResources, operation: Coroutine[Any, Any, None], *, name: str
    ) -> None:
        """Keep one original completion and its outcome after an invocation returns."""

        async def observe() -> None:
            try:
                await operation
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                self.record_failure(scope, error, name=name)

        task = self.watch(observe(), name=name)
        scope.completions.add(task)
        task.add_done_callback(scope.completions.discard)

    @property
    def joined(self) -> bool:
        return self._joined or (not self._started and not self._turns)

    async def _wait_until_deadline(
        self, deadline: float
    ) -> tuple[set[asyncio.Task[Any]], asyncio.CancelledError | None]:
        loop = asyncio.get_running_loop()
        cancellation: asyncio.CancelledError | None = None
        pending: set[asyncio.Task[Any]] = {
            task for task in self._pending | self._turns if not task.done()
        }
        while pending and loop.time() < deadline:
            try:
                await asyncio.wait(pending, timeout=max(0, deadline - loop.time()))
                pending = {task for task in self._pending | self._turns if not task.done()}
            except asyncio.CancelledError as error:
                cancellation = error
                pending = {task for task in self._pending | self._turns if not task.done()}
        return pending, cancellation

    async def aclose(self, *, deadline: float | None = None) -> None:
        """Bound actual join by the host's remaining cancellation-diagnostic budget.

        On expiry the group and exact tasks stay owned. The daemon must leave
        their clients/pools open for its existing hard-exit boundary; a timed
        wait cannot make an unfinished poll or domain close safe to release.
        """
        self._closed = True
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + CANCEL_UNWIND_TIMEOUT_S
        pending, cancellation = await self._wait_until_deadline(deadline)
        errors = [error for _scope, error in self.failures]
        if cancellation is not None:
            errors.insert(0, cancellation)
        if pending:
            error = TimeoutError(
                "hosted resource service join remains unfinished: "
                + ", ".join(sorted(task.get_name() for task in pending))
            )
            logger.opt(exception=error).error(
                "hosted resource service exhausted its join budget; "
                "original tasks and clients remain owned until host hard exit"
            )
            errors.append(error)
        elif self._started and not self._joined:
            await self._tasks.__aexit__(None, None, None)
            self._joined = True
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("late hosted resource failures", errors)


def hosted_resources_settled(resources: HostedTurnResources | None) -> bool:
    """Whether this exact turn scope has discharged every registered domain."""
    return resources is None or not resources.unresolved
