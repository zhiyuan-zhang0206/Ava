"""The tree self-check: runtime proof that the chain is intact (G2 / B7).

The root supervisor's own claims — "these units are my live children" — are not
taken on faith: this loop walks the tree on an interval and probes every
running claim against the OS. A unit that claims to run but whose process is
missing, a corpse, or no longer parented by the root is a broken chain: one
registered event per episode, a gauge for the round, a cumulative count.

Two expressions stay separate (a B7 discipline):

- **broken** — the claim is provably false; alert (one event per episode,
  then a per-round hold line carrying the age).
- **unverifiable** — the read itself failed; a single warning per episode, a
  gauge in the status metrics, never escalated to a break event.

Metrics are the B7 status surface: the chain block plus two injected slots
(attribution coverage / reseeding latency) whose providers arrive with the
platform adapter at wiring — with no provider they read ``unavailable``,
never a fabricated number. Honest non-running states (`stopped` / `backoff` —
the supervisor remediating or policy holding) are NOT broken: revival is the
health runner's domain, the chain check verifies running claims only.

The monitor is mechanism only: nothing builds one yet, and it is
platform-neutral — no concrete platform is ever named here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol, cast

from shared.proc import child_state

_log = logging.getLogger(__name__)

# The two B7 metric slots; providers for exactly these names may be injected
# (an unknown name is a wiring bug, not a silently dropped metric).
ATTRIBUTION_COVERAGE = "attribution_coverage"
RESEEDING_LATENCY_S = "reseeding_latency_s"
_SLOT_NAMES = (ATTRIBUTION_COVERAGE, RESEEDING_LATENCY_S)


def _monotonic() -> float:
    """Wall-independent clock for the episode ages — a module seam so tests can
    advance time (the same pattern as `shared.service_respawn`)."""
    return time.monotonic()


class TreeHost(Protocol):
    """The supervisor slice the self-check reads (duck-typed for stubs)."""

    def tree_view(self) -> dict[str, object]:
        """`{"root_pid": int, "units": [{"id", "state", "pid"}, ...]}` — raw facts."""
        ...


@dataclass(frozen=True, slots=True)
class SelfCheckConfig:
    """Timing policy for the self-check loop."""

    interval_s: float = 60.0
    """Round period — the B7 suggested cadence."""

    def __post_init__(self) -> None:
        if self.interval_s <= 0:
            raise ValueError("interval_s must be positive")


@dataclass(slots=True)
class _ChainState:
    """The walk's episode memory (one broken / one unverifiable episode)."""

    broken_since: float | None = None
    broken_total: int = 0
    last_broken: tuple[str, ...] = ()
    unverifiable_since: float | None = None
    last_unverifiable: tuple[str, ...] = ()
    rounds: int = 0


class TreeSelfCheck:
    """Periodic walk of the tree: running state + parent-child chain integrity.

    One round is a bounded stream of cheap reads (one OS query per running
    unit), and every read is isolated: a failure downgrades that unit to
    `unverifiable` rather than aborting the walk.
    """

    def __init__(
        self,
        host: TreeHost,
        *,
        config: SelfCheckConfig | None = None,
        metrics_providers: Mapping[str, Callable[[], float | None]] | None = None,
    ) -> None:
        providers = dict(metrics_providers or {})
        unknown = set(providers) - set(_SLOT_NAMES)
        if unknown:
            raise ValueError(
                f"unknown metric slot(s) {sorted(unknown)}; the slots are {list(_SLOT_NAMES)}"
            )
        self._host = host
        self._config = config if config is not None else SelfCheckConfig()
        self._providers = providers
        self._state = _ChainState()
        self._task: asyncio.Task[None] | None = None

    def run_once(self) -> None:
        """Walk every unit once, folding the verdicts into the episode state."""
        now = _monotonic()
        self._state.rounds += 1
        view = self._host.tree_view()
        root_pid = cast("int", view["root_pid"])
        units = cast("list[dict[str, object]]", view["units"])
        broken, reasons, unverifiable = self._walk(units, root_pid)
        self._fold_broken(broken, reasons, root_pid, now)
        self._fold_unverifiable(unverifiable, now)
        if not broken and not unverifiable:
            _log.debug("[selfcheck] chain intact (%d unit(s) walked)", len(units))

    def _walk(
        self, units: list[dict[str, object]], root_pid: int
    ) -> tuple[list[str], dict[str, str], list[str]]:
        """One OS read per running unit; a failed read downgrades, never aborts."""
        broken: list[str] = []
        reasons: dict[str, str] = {}
        unverifiable: list[str] = []
        for entry in units:
            unit_id = str(entry["id"])
            if entry["state"] != "running":
                continue
            pid = entry["pid"]
            if pid is None:
                broken.append(unit_id)
                reasons[unit_id] = "running without a recorded pid"
                continue
            try:
                verdict = child_state(cast("int", pid), root_pid)
            except Exception:
                _log.exception("[selfcheck] unit %s: chain read raised", unit_id)
                verdict = "unverifiable"
            if verdict == "attached":
                continue
            if verdict == "unverifiable":
                unverifiable.append(unit_id)
            else:
                broken.append(unit_id)
                reasons[unit_id] = verdict
        return broken, reasons, unverifiable

    def _fold_broken(
        self, broken: list[str], reasons: dict[str, str], root_pid: int, now: float
    ) -> None:
        """Episode fold: one event on enter, a per-round hold age while broken."""
        if broken:
            if self._state.broken_since is None:
                self._state.broken_since = now
                self._state.broken_total += 1
                self._emit_chain_broken(broken, reasons, root_pid)
            else:
                _log.warning(
                    "[selfcheck] tree chain still broken for %.0fs — units: %s",
                    now - self._state.broken_since,
                    ", ".join(broken),
                )
            self._state.last_broken = tuple(broken)
            return
        if self._state.broken_since is not None:
            _log.info(
                "[selfcheck] tree chain intact again after %.0fs",
                now - self._state.broken_since,
            )
        self._state.broken_since = None
        self._state.last_broken = ()

    def _fold_unverifiable(self, unverifiable: list[str], now: float) -> None:
        """Weak expression: one warning per episode, then debug; never an event."""
        if unverifiable:
            if self._state.unverifiable_since is None:
                self._state.unverifiable_since = now
                _log.warning(
                    "[selfcheck] cannot verify %d unit(s) this round (chain read failed): %s",
                    len(unverifiable),
                    ", ".join(unverifiable),
                )
            else:
                _log.debug("[selfcheck] still unverifiable: %s", ", ".join(unverifiable))
            self._state.last_unverifiable = tuple(unverifiable)
            return
        if self._state.unverifiable_since is not None:
            _log.info("[selfcheck] chain reads are verifiable again")
        self._state.unverifiable_since = None
        self._state.last_unverifiable = ()

    def metrics_snapshot(self) -> dict[str, object]:
        """The `metrics` block `Supervisor.status()` embeds (the B7 status surface)."""
        now = _monotonic()
        return {
            "chain": {
                "broken": self._state.broken_since is not None,
                "broken_for_s": self._age(self._state.broken_since, now),
                "broken_units": list(self._state.last_broken),
                "broken_total": self._state.broken_total,
                "unverifiable_units": list(self._state.last_unverifiable),
                "unverifiable_for_s": self._age(self._state.unverifiable_since, now),
                "rounds": self._state.rounds,
            },
            ATTRIBUTION_COVERAGE: self._slot(ATTRIBUTION_COVERAGE),
            RESEEDING_LATENCY_S: self._slot(RESEEDING_LATENCY_S),
        }

    async def start(self) -> None:
        """Run rounds until `stop()` — sleep first, one round per interval."""
        if self._task is not None:
            raise RuntimeError("self-check already started")
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the round loop; safe to call when not started."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._config.interval_s)
            try:
                self.run_once()
            except Exception:
                # `run_once` isolates per unit; this catches a defect in the
                # walk itself. A dead loop would stop the self-proof silently,
                # so the loop survives its own bug — loudly.
                _log.exception("[selfcheck] round raised; continuing")

    def _emit_chain_broken(self, broken: list[str], reasons: dict[str, str], root_pid: int) -> None:
        """The one alert per broken episode — a registered event (loguru `event=`)."""
        from shared.log import logger

        logger.warning(
            "[selfcheck] tree chain broken — {units} no longer verified as live "
            "children of root pid {root_pid} (reasons: {reasons})",
            event="root_chain_broken",
            units=broken,
            reasons=reasons,
            root_pid=root_pid,
        )

    def _slot(self, name: str) -> dict[str, object]:
        provider = self._providers.get(name)
        if provider is None:
            return {"value": None, "state": "unavailable"}
        try:
            value = provider()
        except Exception:
            _log.exception("[selfcheck] metric slot %s: provider raised", name)
            return {"value": None, "state": "error"}
        if value is None:
            return {"value": None, "state": "unavailable"}
        return {"value": value, "state": "ok"}

    @staticmethod
    def _age(since: float | None, now: float) -> float | None:
        return None if since is None else max(0.0, now - since)
