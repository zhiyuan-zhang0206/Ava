"""Bounded, certified stops for one supervisor's units.

Children stay in the permission ancestry: stop captures their native births
before signalling, preserves uncertain custody, and never escalates implicitly.
The family is mixed into `Supervisor` (see `services.ava_root.supervisor`);
these methods rely on its `_config` for the per-unit TERM window and are
otherwise self-contained over the runtime records they are handed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from dataclasses import dataclass
from time import monotonic

from base.native_process.ownership import OwnedProcess, capture_tree, retain_processes
from services.ava_root.custody import ServiceCustody
from services.ava_root.group_scope import (
    capture_group,
    group_empty,
    group_over,
    ownership_retained,
    recorded_living,
    unproven_group,
)
from services.ava_root.manifest import UnitState
from services.ava_root.unit_records import _Generation, _UnitRuntime

_log = logging.getLogger("services.ava_root.supervisor")
"""The supervisor's channel, reused: these lines are its stop operations."""


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """Timing policy for the supervise loop (test-tunable)."""

    stop_timeout_s: float = 10.0
    """Default TERM window per unit; only explicit force permits a later kill.

    A unit whose manifest declares `stop_timeout_s` gets that window instead. The
    window must exceed the longest the unit's own SIGTERM cleanup may run: root
    reads a unit still inside its own bound as unstopped, and refuses."""


class StoppingMixin:
    """The supervisor's bounded, certified stop family.

    A stop captures the unit's native births before signalling, preserves
    uncertain custody, and never escalates implicitly; a refused stop keeps the
    generation and raises for its caller. Mixed into `Supervisor`, which owns
    `_config` (the per-unit TERM window policy used here).
    """

    _config: SupervisorConfig

    async def _stop_unit(self, runtime: _UnitRuntime, *, force: bool = False) -> None:
        """Stop one owned generation; force escalation must be explicit."""
        generation = runtime.generation
        if generation is None:
            runtime.state = UnitState.STOPPED
            return
        await self._stop_generation(runtime, generation, force=force)

    async def _stop_generation(
        self, runtime: _UnitRuntime, generation: _Generation, *, force: bool = False
    ) -> None:
        """Stop captured births; unknown scope keeps custody and refuses success."""
        # Routed by what this stop finds, not by `closing`: a retry after a
        # refused stop whose leader has since exited must not signal by the
        # group number.
        exited = _exited_leader(runtime, generation)
        if exited is not None:
            await self._stop_exited_generation(runtime, generation, *exited, force=force)
            return
        identity, custody = self._capture_posix_stop(runtime, generation)
        await self._stop_posix_generation(runtime, generation, identity, custody, force=force)

    async def _stop_exited_generation(
        self,
        runtime: _UnitRuntime,
        generation: _Generation,
        pgid: int,
        custody: ServiceCustody,
        *,
        force: bool,
    ) -> None:
        """Close what root recorded of a unit whose leader it reaped before this stop.

        Root reaps its own leader, so after that reap the leader is positively
        dead, whether or not root read its birth first, and the watch retained
        the members of group `pgid` it read just after that reap. After that the
        number proves nothing: once those members exit, another program's group
        can carry it. So this stop never signals by the group number: nothing it
        signals, captures or adopts comes from a group listing, which only reads
        the group's session or names it in a refusal. It signals only recorded
        births and their birth-verified descendants (`capture_tree`), each
        through its own birth check; only explicit force escalates. Once none of
        them lives, custody is released when the unit's group is proven over
        (`group_over`): empty when read after the reap or now, its number now
        held as a PID, or the group carrying it now in another session; no such
        group is ever signalled. A group still occupied in root's own session,
        or in one root cannot read, may hold unrecorded processes of the unit:
        custody stays and the stop refuses, naming it. Moving the record aside
        is the operator's word that no process of the unit remains; with no
        recorded birth alive, the generation is dropped without a signal
        (`_drop_moved_aside`).
        """
        await self._await_reap(runtime, generation, pgid, custody)
        window = self._stop_window(runtime)
        deadline = monotonic() + window
        while True:
            # Judged as each poll begins, so a refusal rests on reads taken after the deadline.
            expired = monotonic() >= deadline
            living = recorded_living(generation.tracked)
            if not os.path.lexists(custody.path):
                _drop_moved_aside(runtime, custody, living)
                return
            if living:
                custody.retain(generation.tracked, pgid)
                if expired and not force:
                    raise ownership_retained(runtime.manifest.id, living, pgid, window)
                self._signal_all(living, force=expired and force)
                if expired:
                    force = False
                    deadline = monotonic() + window
            elif group_over(pgid, empty_at_exit=generation.scope_closed_at_exit):
                custody.clear()
                runtime.generation = None
                runtime.state = UnitState.STOPPED
                runtime.last_error = None
                _log.info(
                    "unit %s: recorded births of group %s are gone; custody released",
                    runtime.manifest.id,
                    pgid,
                )
                return
            elif expired:
                raise unproven_group(runtime.manifest.id, pgid, custody)
            await asyncio.sleep(0.05)

    async def _await_reap(
        self,
        runtime: _UnitRuntime,
        generation: _Generation,
        pgid: int,
        custody: ServiceCustody,
    ) -> None:
        """Wait (bounded) for the watch task to reap the exited leader.

        Without that reap root has no group reading taken before the leader's
        number could be reused, so it cannot judge the unit's scope; only a
        record the operator moved aside lets the stop go on without it.
        """
        watch = runtime.watch_task
        if not generation.exited.is_set() and watch is not None and not watch.done():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(generation.exited.wait(), self._stop_window(runtime))
        if not generation.exited.is_set() and os.path.lexists(custody.path):
            raise RuntimeError(
                f"unit {runtime.manifest.id}: leader (pid {pgid}) exited but "
                f"root never observed its reap; custody retained at {custody.path}. Once no "
                "process of this unit remains, move that record aside and retry the stop"
            )

    @staticmethod
    def _capture_posix_stop(
        runtime: _UnitRuntime,
        generation: _Generation,
    ) -> tuple[OwnedProcess, ServiceCustody]:
        identity, custody = generation.identity, generation.custody
        if identity is None or custody is None:
            raise RuntimeError(f"unit {runtime.manifest.id} has unacknowledged native birth")
        retain_processes(generation.tracked, capture_tree(identity))
        custody.retain(generation.tracked, identity.pid)
        generation.closing = True
        return identity, custody

    async def _stop_posix_generation(
        self,
        runtime: _UnitRuntime,
        generation: _Generation,
        identity: OwnedProcess,
        custody: ServiceCustody,
        *,
        force: bool,
    ) -> None:
        """Bounded TERM, then certified closure of the unit's process group.

        Only a stop that found the leader live runs this: the unreaped leader
        reserved the group number until its reap inside this bounded stop,
        which reads the group within that same stop, never after an unbounded
        gap (a stop that finds the leader already reaped is
        `_stop_exited_generation`, which never signals by the group number).
        After the reap, only the kernel reporting that group empty certifies
        the stop: a child forked while the leader handled TERM is still a
        member, so it is captured, signalled like any tracked descendant and
        must exit too. Only explicit force escalates, and only to those
        captured members. A member that calls setsid() leaves the group by
        construction and is not covered.
        """
        self._signal_owned(identity, force=False)
        window = self._stop_window(runtime)
        deadline = monotonic() + window
        while True:
            living = {item for item in generation.tracked if item.live()}
            if not living:
                await generation.exited.wait()
                if group_empty(identity.pid):
                    custody.clear()
                    runtime.generation = None
                    runtime.state = UnitState.STOPPED
                    return
                living = capture_group(generation.tracked, identity.pid)
            for item in living:
                retain_processes(generation.tracked, capture_tree(item))
            custody.retain(generation.tracked, identity.pid)
            expired = monotonic() >= deadline
            if expired and not force:
                raise ownership_retained(runtime.manifest.id, living, identity.pid, window)
            if not identity.live() or expired:
                self._signal_all(living, force=expired and force)
            if expired:
                force = False
                deadline = monotonic() + window
            await asyncio.sleep(0.05)

    def _stop_window(self, runtime: _UnitRuntime) -> float:
        """The TERM window for one unit: what its manifest declares, else the default."""
        declared = runtime.manifest.stop_timeout_s
        return self._config.stop_timeout_s if declared is None else declared

    @staticmethod
    def _signal_owned(identity: OwnedProcess, *, force: bool) -> None:
        identity.send_signal(signal.SIGKILL if force else signal.SIGTERM)

    @classmethod
    def _signal_all(cls, living: set[OwnedProcess], *, force: bool) -> None:
        for item in living:
            cls._signal_owned(item, force=force)


def _exited_leader(
    runtime: _UnitRuntime, generation: _Generation
) -> tuple[int, ServiceCustody] | None:
    """The leader's group number and custody once the leader is positively not
    live (gone, a zombie, or its PID now another birth); None while it runs.

    A leader whose birth root never read had exited before that read: `_spawn`
    records none only on NoSuchProcess, which a running child of root cannot
    raise, and root, its only reaper, reaped it or will. Its group number is
    `proc.pid`, which it was born leading (`process_group=0`), so it is judged
    like any exited leader: by what the watch recorded at its reap, never by
    signalling that number. An identity that cannot be verified keeps custody
    and names the next step.
    """
    identity, custody = generation.identity, generation.custody
    if custody is None:
        raise RuntimeError(f"unit {runtime.manifest.id} has no durable custody")
    if identity is None:
        return generation.proc.pid, custody
    try:
        return None if identity.live() else (identity.pid, custody)
    except RuntimeError as exc:
        raise RuntimeError(
            f"unit {runtime.manifest.id}: cannot confirm whether its recorded birth "
            f"(pid {identity.pid}) still runs: {exc}; custody retained at {custody.path}. "
            f"Inspect pid {identity.pid}: stop it if it is still this unit's process, and retry "
            "the stop once that pid has exited or its identity can be read. Moving the record "
            "aside does not settle a birth root cannot verify"
        ) from exc


def _drop_moved_aside(
    runtime: _UnitRuntime, custody: ServiceCustody, living: set[OwnedProcess]
) -> None:
    """Drop an exited unit's generation whose custody record the operator moved aside.

    Moving it aside declares that no process of the unit remains. Root takes
    that only while no recorded birth lives, and signals nothing; a live
    recorded birth keeps the refusal, since custody is recorded before signals.
    """
    if living:
        raise RuntimeError(
            f"unit {runtime.manifest.id}: custody record {custody.path} was moved aside, but "
            f"recorded births still run (pids {sorted(item.pid for item in living)}). Restore "
            "the record and retry the stop, or stop those processes and retry"
        )
    runtime.generation = None
    runtime.state = UnitState.STOPPED
    runtime.last_error = None
    _log.warning(
        "unit %s: the operator moved the custody record aside (%s); generation dropped "
        "without a signal",
        runtime.manifest.id,
        custody.path,
    )
