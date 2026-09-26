"""Load-bearing timing relationships for hosted agents and cluster maintenance.

Each registered clock is safe only relative to its declared neighbours: deploy
leases outlast progress deadlines, agent leases outlast their renewal interval,
and wedged detection allows the full exec and model retry budget. Independent
HTTP deadlines remain beside their consumers.

Define values in the relevant family module (deploy, stop, or schedule timing),
register them in CLOCKS, and declare their ordering in CONSTRAINTS. Tests check
all defaults; agent-host startup rejects operator overrides that violate the
same relationships. The clock-lattice lint prevents unregistered timing
constants from silently introducing competing deadlines.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import shared.deploy_timing as deploy
import shared.stop_timing as stop
from shared import cluster_lock
from shared.config import settings
from shared.daemon.schedules.schedule_timing import SCHEDULE_STALL_ALERT_AFTER_S
from shared.host_deploy_state import UPDATER_LEASE_TTL_S

# --- schedule supervision family ---------------------------------------------
# Value lives in shared/daemon/schedules/schedule_timing.py: the gateway's schedule manager
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
    # --- deploy family (values in shared/deploy_timing.py / shared/cluster_lock.py) ---
    "NO_PROGRESS_TIMEOUT_S": Clock(
        "deploy",
        lambda: deploy.NO_PROGRESS_TIMEOUT_S,
        "the one definition of 'this host stopped making progress'",
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
        "how often a lease-owning operation re-arms its own deploy lease",
    ),
    "GATEWAY_PREFLIGHT_BUDGET_S": Clock(
        "deploy",
        lambda: deploy.GATEWAY_PREFLIGHT_BUDGET_S,
        "agent-runner start preflight's per-dial gateway budget",
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
    "LEGACY_HOST_ADOPTION_SILENCE_S": Clock(
        "agent-lease",
        lambda: deploy.LEGACY_HOST_ADOPTION_SILENCE_S,
        "renewal silence a legacy NULL row must show before a local successor "
        "may replace its dead owner before lease expiry",
    ),
    "CORPSE_REAP_GRACE_S": Clock(
        "agent-lease",
        lambda: deploy.CORPSE_REAP_GRACE_S,
        "how long a crash-marked idling row may sit dead before the corpse "
        "reaper stamps it terminated ('reaper')",
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
}


CONSTRAINTS: list[Constraint] = [
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
        "AGENT_LEASE_TTL_S",
        "CORPSE_REAP_GRACE_S",
        "a crash-marked corpse must first decay offline (its lease stops being "
        "renewed) before the reaper terminates it — the grace window sits "
        "outside the lease TTL so the visible sequence stays offline-then-dead",
    ),
    Constraint(
        "<",
        "GATEWAY_PREFLIGHT_BUDGET_S",
        "NO_PROGRESS_TIMEOUT_S",
        "one preflight dial can never be what makes a host look stalled",
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
        "never before",
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
        "LEGACY_HOST_ADOPTION_SILENCE_S",
        "AGENT_LEASE_TTL_S",
        "the legacy adoption silence window must land inside the lease it "
        "shortens — at or beyond the TTL a dead predecessor's row could only "
        "ever be adopted by natural expiry, and the evidence gate would be inert",
    ),
    # --- updater family ---
    Constraint(
        "==",
        "UPDATER_LEASE_TTL_S",
        "NO_PROGRESS_TIMEOUT_S",
        "a retained updater lease row shares the whole-run no-progress definition "
        "rather than a competing calibration",
    ),
    # --- wedged family ---
    Constraint(
        ">=",
        "WEDGED_AGE_SEC",
        "EXEC_NODE_TIMEOUT_S + LLM_RETRY_BUDGET_ESTIMATE_S",
        "the wedged threshold must cover a healthy agent's longest legitimate "
        "stall: one exec node plus the LLM retry budget (2400 >= 1200 + 770)",
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
    """Fail fast on any lattice violation. Called at agent-host startup so an
    operator env override that inverts a load-bearing ordering dies loudly on
    the box that lives by these clocks, never silently mid-incident."""
    failures = validate_clock_lattice()
    if failures:
        raise ClockLatticeError("clock lattice violated:\n  " + "\n  ".join(failures))
