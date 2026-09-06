"""The clock lattice — every timing constant that must hold an ORDER relative to
its neighbours, declared in one place.

## What a lattice is, and why it exists

The cluster's lifecycle code is full of timeouts, grace periods, and poll
intervals. Most are independent — a healthcheck probe timeout has no neighbour.
But a subset forms a **partially ordered net**: each clock's value is only safe
*because* of its position relative to other clocks. Examples:

- agent spawn: `BOOT_STALL_SEC < LAUNCH_CONFIRM_TIMEOUT_SEC < BOOT_BUDGET_SEC
  < BOOT_REAP_GRACE_SEC` — a stalled child must die before the launcher
  adjudicates, and the launcher's wait must never outlive the reaper's grace
  (the 2026-07-30 spawn incident was this chain inverted).
- deploy: `NO_PROGRESS_TIMEOUT_S < LOCK_TTL_S` — the crash-reclaim bound must
  outlast the no-progress judgment, or a slow-but-alive rollout loses its
  protection mid-operation (the 2026-07-29 incident).
- agent lease: `AGENT_LEASE_TTL_S = 10 x AGENT_LEASE_RENEW_INTERVAL_S` and
  `CONTROLLER_SCAN_INTERVAL_S < AGENT_LEASE_RENEW_INTERVAL_S` — a healthy agent
  renews every 60 s and the reaper scans every 30 s, so a transient renewal
  blip never reads as death.

These orderings used to exist only as prose in docstrings, cross-referencing each
other, pinned by a few scattered tests. Changing one constant without respecting
its neighbours silently re-opened a race — which is exactly how the 2026-07-30
spawn incident stayed hidden ("two defaults in two files with an ordering between
them, described only in prose"). This module is the **single authority** for the
topology: every lattice clock is registered here with its value source and its
neighbourhood, every ordering is declared here with its intent, and
`validate_clock_lattice()` checks the live values against the declared orderings.

## What belongs in the lattice

A clock belongs here when **its safety depends on another clock's value**, or
vice versa. Independent clocks (a per-call HTTP timeout, a healthcheck probe
window) do not — they live beside their consumer, and `scripts/lint_clock_lattice.py`
only flags names that use lattice vocabulary (STALL / CONFIRM / GRACE / REAP /
WEDGED / NO_PROGRESS / LOCK_TTL / UPDATER_LEASE / SCAN_INTERVAL / ...).

## Adding a clock

1. Define its value in its family module (`shared/boot_timing.py` for the boot
   family, `shared/deploy_timing.py` for the deploy family,
   `shared/stop_timing.py` for the stop family, `shared/schedule_timing.py` for
   the schedule-supervision family) or as a settings field when the operator
   must be able to override it.
2. Register it in `CLOCKS` with a one-line `doc`.
3. Declare every ordering it participates in as a `Constraint`, with the intent
   in `doc` — if it has no neighbours, it does not belong in the lattice.
4. The topology test (`tests/shared/test_timing_topology.py`) and the runtime
   check (restarter startup) now cover it automatically.

## Enforcement

- **Tests** (`tests/shared/test_timing_topology.py`): every constraint is
  asserted against the settings defaults, plus one breaking test per constraint
  kind to prove the checker itself works.
- **Runtime** (`services/restarter/daemon.py`): `assert_clock_lattice()` runs at
  startup, so an operator env override that inverts a load-bearing ordering
  fails fast on the box that lives by these clocks — never silently mid-incident.
- **Lint** (`scripts/lint_clock_lattice.py`): lattice-vocabulary constants may
  only be defined in the family modules / settings, so a future bare
  `_SOME_REAP_GRACE_S = 100` in a new module is caught at review time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import shared.boot_timing as boot
import shared.deploy_timing as deploy
import shared.stop_timing as stop
from shared import cluster_lock
from shared.config import settings
from shared.host_deploy_state import UPDATER_LEASE_TTL_S
from shared.schedule_timing import SCHEDULE_STALL_ALERT_AFTER_S

# --- restarter family: the controller scan cadence ---------------------------
# Shared by the three restarter controllers that scan agents (respawn reaper /
# wedged / crash-resurrect), which used to each define their own `_SCAN_INTERVAL_S
# = 30.0`. Must stay below `AGENT_LEASE_RENEW_INTERVAL_S` (60 s): the reaper runs
# between renewals, so a single missed renewal (a DB hiccup) never reads as death
# against the 600 s TTL.
CONTROLLER_SCAN_INTERVAL_S = 30.0


# --- schedule supervision family ---------------------------------------------
# Value lives in shared/schedule_timing.py: the gateway's schedule manager
# imports it from there directly, without the lattice module's agent/sandbox
# settings reads. The NO_PROGRESS_TIMEOUT_S ordering is declared in
# CONSTRAINTS below.


# --- wedged family: the derivation's components ------------------------------
# The wedged threshold is derived, not arbitrary: an agent holding an unconsumed
# pending inbound for this long is presumed wedged because a healthy agent's
# longest legitimate stall is one exec node (1200 s) plus the LLM retry budget.
EXEC_NODE_TIMEOUT_S = settings.sandbox.exec_node_timeout_seconds

# Historical estimate (~770 s) of the LLM retry budget under the retry config;
# not a single settings field, so stated here as the constant the wedged default
# was rounded up from (wedged.py: "exec timeout is 1200s + LLM retry budget
# ~770s ≈ 2000s, rounded up for margin").
LLM_RETRY_BUDGET_ESTIMATE_S = 770.0


@dataclass(frozen=True)
class Clock:
    """One lattice clock: a name, a family, a value source, and why it exists."""

    family: str
    value: float | Callable[[], float]
    doc: str

    def get(self) -> float:
        return self.value() if callable(self.value) else self.value


@dataclass(frozen=True)
class Constraint:
    """One declared ordering between lattice clocks (or expressions of them).

    `lhs` / `rhs` name a clock in `CLOCKS`, optionally scaled or summed:
    `"NAME"`, `"N * NAME"`, or `"NAME + NAME"`. `kind` is one of
    `<`, `<=`, `==`, `>=`.
    """

    kind: str
    lhs: str
    rhs: str
    doc: str


CLOCKS: dict[str, Clock] = {
    # --- boot family (values in shared/boot_timing.py) ---
    "BOOT_STALL_SEC": Clock(
        "boot",
        lambda: boot.BOOT_STALL_SEC,
        "child boot watchdog: how long one boot phase may stall before the child kills itself",
    ),
    "LAUNCH_CONFIRM_TIMEOUT_SEC": Clock(
        "boot",
        lambda: boot.LAUNCH_CONFIRM_TIMEOUT_SEC,
        "launcher confirm window: how long a spawn may remain unclaimed before force-terminate",
    ),
    "BOOT_BUDGET_SEC": Clock(
        "boot",
        lambda: boot.BOOT_BUDGET_SEC,
        "child's hard ceiling on the whole pre-claim boot",
    ),
    "BOOT_REAP_GRACE_SEC": Clock(
        "boot",
        lambda: boot.BOOT_REAP_GRACE_SEC,
        "restarter's grace before reaping an unclaimed idling row",
    ),
    # --- deploy family (values in shared/deploy_timing.py / shared/cluster_lock.py) ---
    "NO_PROGRESS_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.NO_PROGRESS_TIMEOUT_S,
        "the one definition of 'this host stopped making progress'",
    ),
    "PHASE_B_ABSOLUTE_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.PHASE_B_ABSOLUTE_TIMEOUT_S,
        "the Phase-B poll's alias for the whole-run no-progress deadline",
    ),
    "STAGE_NO_PROGRESS_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.STAGE_NO_PROGRESS_TIMEOUT_S,
        "how long one updater stage may be in flight before host reaper and Phase-B "
        "poll call it no-progress",
    ),
    "LEASE_ARM_GRACE_S": Clock(
        "deploy",
        lambda: deploy.LEASE_ARM_GRACE_S,
        "how long a Phase-B poll reads paused-with-no-lease as 'the updater has "
        "not armed yet' before treating it as a provable stop",
    ),
    "CONVERGING_POLL_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.CONVERGING_POLL_TIMEOUT_S,
        "how long the Phase-B poll keeps waiting on a host that is alive and making "
        "progress before handing its convergence to the settle hold",
    ),
    "HARVEST_GRACE_S": Clock(
        "deploy",
        lambda: deploy.HARVEST_GRACE_S,
        "one short wait before the Phase-B harvest re-probe of a converged host",
    ),
    "LOCK_TTL_S": Clock(
        "deploy",
        lambda: cluster_lock.LOCK_TTL_S,
        "deploy lease crash-reclaim bound",
    ),
    "SETTLE_TTL_S": Clock(
        "deploy",
        lambda: cluster_lock.SETTLE_TTL_S,
        "settle hold after an orchestration exits with hosts still converging",
    ),
    "LEASE_RENEW_INTERVAL_S": Clock(
        "deploy",
        lambda: deploy.LEASE_RENEW_INTERVAL_S,
        "how often the orchestration process re-arms its own lease",
    ),
    "GATEWAY_READY_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.GATEWAY_READY_TIMEOUT_S,
        "how long Phase B waits for its own gateway to serve before fan-out",
    ),
    "UV_SYNC_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.UV_SYNC_TIMEOUT_S,
        "hard ceiling on one production uv sync inside the updater",
    ),
    "GATEWAY_PREFLIGHT_BUDGET_S": Clock(
        "deploy",
        lambda: deploy.GATEWAY_PREFLIGHT_BUDGET_S,
        "updater preflight's per-dial gateway budget",
    ),
    "SERVICE_READY_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.SERVICE_READY_TIMEOUT_S,
        "how long `ava start` waits for freshly launched services to pass liveness",
    ),
    "NON_CRITICAL_SERVICE_READY_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.NON_CRITICAL_SERVICE_READY_TIMEOUT_S,
        "how long `ava start` waits for a non-critical service before it stops "
        "blocking the start (reported and alerted instead)",
    ),
    "ORCHESTRATION_OWNER_WAIT_S": Clock(
        "deploy",
        lambda: deploy.ORCHESTRATION_OWNER_WAIT_S,
        "server-side wait for a detached orchestration to publish its durable UI owner",
    ),
    "CLUSTER_DISPATCH_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.CLUSTER_DISPATCH_TIMEOUT_S,
        "client-side bound that must outlive orchestration ownership publication",
    ),
    # --- agent-lease family (values in shared/deploy_timing.py) ---
    "AGENT_LEASE_TTL_S": Clock(
        "agent-lease",
        lambda: deploy.AGENT_LEASE_TTL_S,
        "agents_meta.lease_expires_at TTL (crash-reclaim bound)",
    ),
    "AGENT_LEASE_RENEW_INTERVAL_S": Clock(
        "agent-lease",
        lambda: deploy.AGENT_LEASE_RENEW_INTERVAL_S,
        "how often a healthy agent renews its lease",
    ),
    "AGENT_LEASE_ZOMBIE_GRACE_S": Clock(
        "agent-lease",
        lambda: deploy.AGENT_LEASE_ZOMBIE_GRACE_S,
        "post-outage window during which the lease-zombie reaper spares paused agents",
    ),
    "AGENT_LEASE_SUSPEND_GAP_S": Clock(
        "agent-lease",
        lambda: deploy.AGENT_LEASE_SUSPEND_GAP_S,
        "restarter wall-clock tick gap that signals a suspend, arming the grace",
    ),
    # --- restarter family ---
    "CONTROLLER_SCAN_INTERVAL_S": Clock(
        "restarter",
        CONTROLLER_SCAN_INTERVAL_S,
        "restarter controllers' agent-scan cadence",
    ),
    "RESTARTER_POLL_INTERVAL_S": Clock(
        "restarter",
        lambda: settings.daemon.restarter_poll_interval_seconds,
        "how often the restarter checks restarting-agent rows",
    ),
    # --- schedule supervision family ---
    "SCHEDULE_STALL_ALERT_AFTER_S": Clock(
        "schedule-supervision",
        SCHEDULE_STALL_ALERT_AFTER_S,
        "how long an enabled non-completed schedule may remain sessionless before alerting",
    ),
    # --- updater family ---
    "UPDATER_LEASE_TTL_S": Clock(
        "updater",
        lambda: UPDATER_LEASE_TTL_S,
        "how long a crashed updater's lease keeps its host reading 'converging'",
    ),
    # --- wedged family ---
    "WEDGED_AGE_SEC": Clock(
        "wedged",
        lambda: settings.daemon.wedged_agent_inbound_age_seconds,
        "running-agent age of an unconsumed pending inbound that presumes an agent wedged",
    ),
    "IDLING_WEDGED_AGE_SEC": Clock(
        "wedged",
        lambda: settings.daemon.wedged_idling_agent_inbound_age_seconds,
        "idling-agent age of an unconsumed pending inbound that presumes its claim loop wedged",
    ),
    "IDLE_CLAIM_BACKSTOP_S": Clock(
        "wedged",
        lambda: settings.agent.db_notify_wait_timeout_seconds,
        "process idle claim loop's Redis-wake fallback SELECT cadence",
    ),
    "EXEC_NODE_TIMEOUT_S": Clock(
        "wedged",
        lambda: EXEC_NODE_TIMEOUT_S,
        "graph-level exec node timeout (wedged derivation component)",
    ),
    "LLM_RETRY_BUDGET_ESTIMATE_S": Clock(
        "wedged",
        LLM_RETRY_BUDGET_ESTIMATE_S,
        "historical estimate of the LLM retry budget (wedged derivation component)",
    ),
    # --- stop family (values in shared/stop_timing.py) ---
    "CANCEL_UNWIND_TIMEOUT_S": Clock(
        "stop",
        lambda: stop.CANCEL_UNWIND_TIMEOUT_S,
        "cancel unwind: how long a hosted runner's cancel waits for the host to emit host_turn_uncancellable",
    ),
    "CLOCK_READ_TIMEOUT_S": Clock(
        "stop",
        lambda: stop.CLOCK_READ_TIMEOUT_S,
        "stuck-clock read: how long the cancel path may wait for one clock read",
    ),
    "REAP_KILL_WINDOW_S": Clock(
        "stop",
        lambda: stop.REAP_KILL_WINDOW_S,
        "stop path's force-kill window: when the hosted runner is SIGKILLed if it has not exited",
    ),
}


CONSTRAINTS: list[Constraint] = [
    # --- boot chain: the child dies before the launcher adjudicates, and the
    # launcher's wait never outlives the reaper ---
    Constraint(
        "<",
        "BOOT_STALL_SEC",
        "LAUNCH_CONFIRM_TIMEOUT_SEC",
        "stalled child must have killed itself before the launcher first looks, "
        "so a live process means a progressing boot",
    ),
    Constraint(
        "<",
        "LAUNCH_CONFIRM_TIMEOUT_SEC",
        "BOOT_REAP_GRACE_SEC",
        "the reaper must not take a row the launcher is still legitimately waiting "
        "on (2026-07-30 spawn incident: confirm raised without the grace)",
    ),
    Constraint(
        "<",
        "BOOT_STALL_SEC",
        "BOOT_BUDGET_SEC",
        "the budget ceilings the stall window; a budget below the stall window "
        "would make the stall window unreachable",
    ),
    Constraint(
        "<",
        "BOOT_BUDGET_SEC",
        "BOOT_REAP_GRACE_SEC",
        "the child must be gone before the reaper could claim its row — a live, "
        "progressing child must never have its row reaped out from under it",
    ),
    # --- deploy family: the lease must not expire before the operation it
    # protects can finish ---
    Constraint(
        "<",
        "NO_PROGRESS_TIMEOUT_S",
        "LOCK_TTL_S",
        "the crash-reclaim bound must outlast the no-progress judgment, or a "
        "slow-but-alive rollout loses its lease mid-operation (2026-07-29 incident)",
    ),
    Constraint(
        "<",
        "STAGE_NO_PROGRESS_TIMEOUT_S",
        "NO_PROGRESS_TIMEOUT_S",
        "the stage judgment must fire while the whole-run patience still holds, or "
        "a host stuck in one stage outlasts the poll that exists to wait for it",
    ),
    Constraint(
        "<",
        "CONVERGING_POLL_TIMEOUT_S",
        "NO_PROGRESS_TIMEOUT_S",
        "the converging patience spends only PART of the absolute no-progress "
        "deadline: a host that is alive and making progress is handed to the settle "
        "hold once this elapses, and a value at or beyond the whole-run bound would "
        "make that early exit unreachable",
    ),
    Constraint(
        "==",
        "PHASE_B_ABSOLUTE_TIMEOUT_S",
        "NO_PROGRESS_TIMEOUT_S",
        "the advertised Phase-B absolute deadline is an alias for the whole-run "
        "no-progress definition, not an independent calibration",
    ),
    Constraint(
        "<",
        "HARVEST_GRACE_S",
        "NO_PROGRESS_TIMEOUT_S",
        "the harvest re-probe reads the host's outcome through the same "
        "fresh-idle window the no-progress judgment defines, so its grace must "
        "land far inside that window — a grace at or beyond it would always "
        "find the reading stale and silently drop a converged host's completed "
        "stage breakdown",
    ),
    Constraint(
        "<",
        "GATEWAY_READY_TIMEOUT_S",
        "LOCK_TTL_S",
        "the ready wait sits BEFORE the Phase-B poll with no renewal task armed, "
        "so it spends lease time and must stay well under the TTL",
    ),
    Constraint(
        "<",
        "GATEWAY_PREFLIGHT_BUDGET_S",
        "NO_PROGRESS_TIMEOUT_S",
        "one preflight dial can never be what makes a host look stalled",
    ),
    Constraint(
        "<",
        "UV_SYNC_TIMEOUT_S",
        "STAGE_NO_PROGRESS_TIMEOUT_S",
        "the bounded sync's self-termination lands before the stage no-progress "
        "judgment reaps the updater, so the updater's own recovery ladder wins the "
        "race on its own stage",
    ),
    Constraint(
        "<",
        "UV_SYNC_TIMEOUT_S",
        "NO_PROGRESS_TIMEOUT_S",
        "a hung sync must fail itself into a terminal outcome before the host is "
        "judged stalled, so the updater's recovery ladder beats the stalled-updater reap",
    ),
    Constraint(
        "==",
        "SERVICE_READY_TIMEOUT_S",
        "GATEWAY_READY_TIMEOUT_S",
        "same physical job (a local daemon binds its port) and same value — but "
        "deliberately separate constants with different observers and escalation "
        "paths; retuning one is not automatically a reason to retune the other",
    ),
    Constraint(
        "<",
        "NON_CRITICAL_SERVICE_READY_TIMEOUT_S",
        "SERVICE_READY_TIMEOUT_S",
        "the tiered gate's premise: the non-critical window must end long before "
        "the critical bound, so a healthy start is never held to the long number "
        "by a straggling non-critical daemon",
    ),
    Constraint(
        "==",
        "SETTLE_TTL_S",
        "NO_PROGRESS_TIMEOUT_S",
        "the settle hold shares the whole-run no-progress definition — it lapses "
        "when the host it waits for has outlived the longest legitimate leg, "
        "never before; the reaper's earlier per-stage judgment "
        "(STAGE_NO_PROGRESS_TIMEOUT_S) ends the hold through the convergence "
        "path instead",
    ),
    Constraint(
        "<",
        "ORCHESTRATION_OWNER_WAIT_S",
        "CLUSTER_DISPATCH_TIMEOUT_S",
        "the detached child must publish ownership before the dispatching client "
        "can time out and invite a duplicate submission",
    ),
    # --- schedule supervision family ---
    Constraint(
        "<",
        "NO_PROGRESS_TIMEOUT_S",
        "SCHEDULE_STALL_ALERT_AFTER_S",
        "the schedule-silence alert must outlive a legitimate rollout's "
        "no-progress window, so normal stop-the-world service churn stays quiet",
    ),
    # --- agent-lease family ---
    Constraint(
        "==",
        "AGENT_LEASE_TTL_S",
        "10 * AGENT_LEASE_RENEW_INTERVAL_S",
        "TTL = 10x the renewal interval, so a transient renewal blip never reads "
        "as death against the reaper cadence",
    ),
    Constraint(
        "<",
        "CONTROLLER_SCAN_INTERVAL_S",
        "AGENT_LEASE_RENEW_INTERVAL_S",
        "the reaper scans between renewals (30 s < 60 s): a healthy agent renews "
        "before the next scan can ever see a stale lease",
    ),
    Constraint(
        "<",
        "AGENT_LEASE_RENEW_INTERVAL_S",
        "AGENT_LEASE_ZOMBIE_GRACE_S",
        "the post-outage grace must outlast the renew cadence (60 s < 180 s): a "
        "paused agent renews within one interval of the DB returning, so its "
        "renewal lands inside the window — the grace exists to give it exactly "
        "that chance (2026-08-08 audit P1-2)",
    ),
    # --- updater family ---
    Constraint(
        "==",
        "UPDATER_LEASE_TTL_S",
        "NO_PROGRESS_TIMEOUT_S",
        "the updater lease must not expire before the no-progress judgment that "
        "reaps a hung updater — a slow-but-alive updater is never reaped mid-work",
    ),
    # --- wedged family ---
    Constraint(
        ">=",
        "WEDGED_AGE_SEC",
        "EXEC_NODE_TIMEOUT_S + LLM_RETRY_BUDGET_ESTIMATE_S",
        "the wedged threshold must cover a healthy agent's longest legitimate "
        "stall: one exec node plus the LLM retry budget (2400 >= 1200 + 770)",
    ),
    Constraint(
        ">=",
        "IDLING_WEDGED_AGE_SEC",
        "2 * IDLE_CLAIM_BACKSTOP_S",
        "an idling claim loop gets at least two fallback SELECT rounds before wedged recovery; "
        "it is deliberately independent of running-turn exec and LLM budgets",
    ),
    # --- stop family: the hosted runner's shutdown waits must fit inside the
    # stop path's force-kill window, or the host is killed before it can emit
    # host_turn_uncancellable (issue #196) ---
    Constraint(
        "<",
        "CANCEL_UNWIND_TIMEOUT_S + CLOCK_READ_TIMEOUT_S",
        "REAP_KILL_WINDOW_S",
        "cancel unwind + stuck-clock read must finish inside the stop path's "
        "force-kill window (5 + 2 < 15), so the uncancellable report is never "
        "killed mid-emission",
    ),
]


def _resolve(expr: str) -> float:
    """Resolve `"NAME"`, `"N * NAME"`, or `"NAME + NAME"` against `CLOCKS`."""
    if " + " in expr:
        return sum(_resolve(part) for part in expr.split(" + "))
    if " * " in expr:
        n, name = expr.split(" * ", 1)
        return float(n) * CLOCKS[name].get()
    return CLOCKS[expr].get()


def validate_clock_lattice() -> list[str]:
    """Check every declared ordering against the LIVE clock values.

    Returns a list of human-readable violations (empty when the lattice holds).
    Values are read lazily at call time, so settings / env overrides and test
    monkeypatches are all reflected.
    """
    failures: list[str] = []
    for c in CONSTRAINTS:
        left, right = _resolve(c.lhs), _resolve(c.rhs)
        holds = {
            "<": left < right,
            "<=": left <= right,
            "==": left == right,
            ">=": left >= right,
        }[c.kind]
        if not holds:
            failures.append(f"{c.lhs} {c.kind} {c.rhs}: {left} vs {right} — {c.doc}")
    return failures


class ClockLatticeError(RuntimeError):
    """Raised when live clock values violate a declared lattice ordering."""


def assert_clock_lattice() -> None:
    """Fail fast on any lattice violation. Called at restarter startup so an
    operator env override that inverts a load-bearing ordering dies loudly on
    the box that lives by these clocks, never silently mid-incident."""
    failures = validate_clock_lattice()
    if failures:
        raise ClockLatticeError("clock lattice violated:\n  " + "\n  ".join(failures))
