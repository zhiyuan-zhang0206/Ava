"""Phase B's precondition, checked before Phase B rather than assumed by it.

## The ordering bug this closes

Every agent-runner's self-update begins with a **validate-before-kill preflight**
(`cli.commands._update_agent_runner`, step 3): before it stops a single service it
proves it can still reach the gateway, because a host that stops and then cannot come
back is unrecoverable without hands. A runner that fails that probe therefore does the
right thing and *declines*, returning `RESTART_DECLINED_EXIT_CODE` with its services
untouched.

Phase B used to fan that op out with no check at all that the gateway it makes each
runner depend on was serving yet. The gateway's local leg ends with `ava start` in a
child process, and that child's own readiness wait (`_START_READINESS_TIMEOUT_S`,
15 s) is **best-effort — it returns anyway on expiry** and `ava start` still exits 0.
So `rc == 0` from the local update means "the start process exited cleanly", never
"the gateway is serving". Fanning out on that is a race, and prod lost it repeatedly:
a runner's updater log for the 2026-07-29 05:09 rollout ends

    ✗ gateway unreachable at http://<the runner's gateway address>:8000: [Errno 61] Connection refused
    ✗ refusing self-update: preflight probes failed — host still serving
    [session-exit] rc=3

~3 s after it started, ~39 s into the rollout — after the start child had already
spent its 15 s wait and exited 0. Every "slow host" in those rollouts was a *failed*
host, and the runner was blameless: it was told to update before the thing it must
reach existed.

## What has to be reachable, and when it becomes so

Three instants get conflated; only the middle one matters here.

1. **The gateway process is up** — the session exists. Says nothing: uvicorn has not
   bound a port yet, and a process that is about to die of `assert_schema_current`
   looks identical.
2. **The gateway is serving `GATEWAY_PROBE_PATH`** — the instant this gate waits for,
   because it is the instant the runner's preflight needs. Nothing else in the flow
   observes it: `ava start`'s wait is local, unauthenticated, non-fatal and 15 s.
3. **The gateway has finished migrating** — *earlier* than (2), not later.
   `ava start` applies migrations before it launches the session, and
   `gateway.app.main` asserts schema-current before `uvicorn.run`. So serving implies
   migrated and there is no third wait to write.

The gate probes `gateway_api_base()` — the same resolver every runner uses, and by
design not a loopback shortcut on a gateway box either — with the same endpoint,
headers and dial as the preflight (`probe_gateway_once`). Same question, same answer.

## Why the wait lives here

The alternative was a bounded retry inside the runner's preflight. That is the weaker
fix *for the ordering bug* and was rejected as a replacement for this gate: it leaves
the ordering bug in place and merely makes it usually survivable, it spends the retry
on *every* runner in parallel instead of once at the gateway, and it stretches the
window in which a host sits on a new checkout with old processes running — the exact
half-transitioned state the deploy lease exists to protect. Reordering Phase B before
the gateway's local leg is not an option at all: runners assert schema-current at boot,
so they cannot come up on the new commit until the gateway has migrated.

A bounded preflight budget now exists anyway (`_probe_gateway_or_die`,
`GATEWAY_PREFLIGHT_BUDGET_S`), and it does not weaken any of that. It answers a
different question — not "was the gateway ready before Phase B?", which is this gate's
and stays here, but "is one refused packet, *after* this gate saw a 200, enough to
declare the gateway dead?". Prod answered no on 2026-08-01 (issue #1151): a 9 s hole
cost two runners a 15-minute settle-TTL stall. The hole itself was self-inflicted and
is closed at its source (`_phase_b_targets`); the budget is what remains for holes this
rollout did not open.

Checking it here also makes the dependency **checkable**: one probe, at one place,
against the same URL the dependents use, with a verdict that reaches the rollout
report.

## What expiry means

`GATEWAY_READY_TIMEOUT_S` bounds the wait, and the healthy path pays for none of it —
a gateway that is already serving is one probe (a few ms) and Phase B proceeds
immediately. The bound is only ever spent by a gateway that is alive and has bound
nothing, because every failure a further wait cannot fix ends the gate at once:

- `GATEWAY_GONE` — the gateway session is no longer alive, so nothing will ever bind.
- `OFF_BOX_UNREACHABLE` — it answers the local health probe but not the address the
  cluster dials. The gateway is fine; the path to it is not (the recurring macOS
  app-firewall rule orphaned by a `uv python` bump, issue #949). Waiting cannot fix a
  firewall rule, and it is worth saying which of the two broke. The verdict does not
  stop at naming the two candidate causes either: `_firewall_attribution` asks
  `cli.commands._converge_firewall` — the same unprivileged audit `ava converge`
  reports from — which of them it actually is, and prints the exact privileged repair
  when it is the firewall.
- `REFUSED` — it answers with a non-200, non-5xx status (401/403/404): a credential or
  route mismatch, which no amount of waiting resolves.

Expiry (`TIMED_OUT`) and every early failure are all `RolloutOutcome.INCOMPLETE` at
the call site, never CLEAN and never ABORTED: the gateway has migrated and the pin has
advanced, so the cluster IS mid-transition, and re-running `ava cluster update` into it is the
collision `_print_rollout_aftermath` warns about. The runners are not told to update,
so no host is left with a moved checkout — but they stay on the old commit against a
migrated schema, and the deploy lease must keep holding them until their watchdogs
converge them to the pin.
"""

from __future__ import annotations

import sys
import time
from enum import StrEnum
from urllib.parse import urlsplit

from cli.commands._repo import GATEWAY_PROBE_PATH, GatewayProbe, _repo_root, probe_gateway_once
from shared.deploy_timing import GATEWAY_READY_TIMEOUT_S
from shared.paths import ava_home
from shared.port_preflight import listeners_on, process_mentions

# One dial's timeout, and the gap between dials. Both small: the gate's job is to
# notice the gateway the moment it starts serving, and a refused connection answers
# instantly anyway. The dial timeout matters only for the blackholed-path case, which
# the local cross-check ends after two rounds regardless.
_READY_DIAL_TIMEOUT_S = 5.0
_READY_PROBE_INTERVAL_S = 1.0

# How many consecutive passes must agree before an *inferred* early exit is taken.
# `SERVING` (a 200) and `REFUSED` (an authenticated 4xx) are direct evidence and end
# the gate on sight. `GATEWAY_GONE` and `OFF_BOX_UNREACHABLE` are inferred from a
# second observation — a session lookup, a loopback probe — taken a moment after the
# dial they explain, so one contrary reading is a race rather than evidence. Two, one
# interval apart, costs `_READY_PROBE_INTERVAL_S` and not a rollout. Same rule, for
# the same reason, as `_STALL_CONFIRMATIONS` in the Phase-B poll.
_EARLY_EXIT_CONFIRMATIONS = 2

# Cadence of the "still waiting" progress line. At `_READY_PROBE_INTERVAL_S` per pass
# the full bound would otherwise print ~180 lines into the one log an operator reads
# to find real problems.
_WAIT_NOTE_INTERVAL_S = 10.0


class GatewayReadiness(StrEnum):
    """Verdict of the pre-Phase-B gateway readiness gate.

    Only `SERVING` allows the fan-out. The other four are all
    `RolloutOutcome.INCOMPLETE` to the caller and differ in the operator's next move,
    which is exactly why they are four values and not one boolean — the same reason
    PR #937 split `degraded` into `POLL_CONVERGING` / `POLL_STALLED`.
    """

    SERVING = "serving"
    GATEWAY_GONE = "gateway_gone"
    OFF_BOX_UNREACHABLE = "off_box_unreachable"
    REFUSED = "refused"
    TIMED_OUT = "timed_out"


def _gateway_session_alive() -> bool:
    """Is this host's gateway session still alive?

    The gate's fast exit for the case no wait can fix. A session that exited took the
    uvicorn with it — commonly `assert_schema_current` failing at boot, or `[Errno 48]
    Address already in use` — and the port will never be bound. Read through the
    `cli.commands` namespace so it shares the seam `ava status` uses.
    """
    import cli.commands as _ns
    from cli.commands._repo import session_name

    return _ns._has_session(session_name("gateway"))


def _serving_locally() -> bool:
    """Is THIS unit's gateway answering on loopback?

    Only asked once the cluster-facing dial has already failed, to attribute the
    failure: serving here but not there means the process is healthy and the network
    path to it is not. That attribution has to be identity-verified or it is worse
    than no attribution — another cluster's gateway on this port would answer 200 and
    send the operator hunting a network fault that does not exist. `probe_home` on
    `gateway_health_url` is the same check `ava status` and the gateway healthcheck
    run, so this adds no new definition of "the gateway is up".
    """
    from shared.config import settings
    from shared.daemon_health import probe_home

    return probe_home(settings.services.gateway_health_url).alive


def _classify(probe: GatewayProbe) -> GatewayReadiness | None:
    """Map one probe to a terminal verdict, or None for "keep waiting".

    None is returned for exactly two readings, and both are the *expected* readings
    mid-boot: no answer at all (`status is None` — uvicorn has not bound yet), and a
    5xx (the app is up but not ready). Everything else is terminal.
    """
    if probe.status == 200:
        return GatewayReadiness.SERVING
    if probe.status is None or probe.status >= 500:
        return None
    return GatewayReadiness.REFUSED


def _gateway_listener_owned(url: str) -> bool:
    """Whether the process answering `url`'s port is THIS unit's gateway.

    A 200 from GATEWAY_PROBE_PATH is the gate's direct evidence, but it says
    nothing about WHICH process answered: another cluster's gateway, a stale
    test server or a leftover daemon on the same port all answer 200 and would
    bless a Phase B that must not fire — and the orphan-sweep failure class
    this closes (Task #965) is exactly an old process still holding the port.
    Resolve the listener pid(s) on the dialed port and apply the unit-ownership
    predicate (argv / exe / cwd mentions this repo or home — the same rule the
    start preflight and `ava stop`'s orphan sweep apply). A listener that is
    positively foreign -> False, so the gate refuses instead of blessing a
    wrong responder. Inconclusive reads (no listener found, unreadable scan —
    the test env, a macOS AccessDenied fallback) return True: the probe itself
    is the primary evidence, this check is the defense against a
    positively-wrong one.
    """
    try:
        port = urlsplit(url).port
    except ValueError:
        return True
    if port is None:
        return True
    pids = listeners_on(port)
    if not pids:
        return True  # inconclusive — the probe spoke for itself
    markers = (str(_repo_root().resolve()), str(ava_home().resolve()))
    return all(process_mentions(pid, markers) for pid in pids)


def await_gateway_serving(
    *, timeout_s: float = GATEWAY_READY_TIMEOUT_S
) -> tuple[GatewayReadiness, str]:
    """Block until this cluster's gateway serves the endpoint Phase B's dependents
    need, or until a verdict says waiting is pointless. Returns (verdict, detail).

    Called between the gateway's local leg and the Phase-B fan-out. Costs one probe on
    the healthy path — the caller's own docstring and the tests both pin that a ready
    gateway adds no latency, because a readiness gate that taxes every good rollout is
    a bad trade even when it is correct.
    """
    from shared.machine import GatewayApiBaseMissing, gateway_api_base

    try:
        url = gateway_api_base()
    except GatewayApiBaseMissing as exc:
        # No configured address means no host can reach this gateway, including the
        # runners about to be told to depend on it. Terminal, and not a timeout.
        return GatewayReadiness.REFUSED, str(exc)

    print(f"\n→ gateway readiness: {url}{GATEWAY_PROBE_PATH} must serve before Phase B")
    deadline = time.monotonic() + timeout_s
    inferred: tuple[GatewayReadiness, str] | None = None
    streak = 0
    noted_at = 0.0
    while True:
        probe = probe_gateway_once(url, timeout_s=_READY_DIAL_TIMEOUT_S)
        verdict = _classify(probe)
        if verdict is GatewayReadiness.SERVING:
            # The 200 is direct evidence SOMETHING serves the probe; verify it
            # is THIS unit's gateway (pid ownership), or the rollout would fan
            # out against a wrong responder (Task #965 — the orphan class).
            if not _gateway_listener_owned(url):
                return (
                    GatewayReadiness.REFUSED,
                    f"the process answering {url} is not this unit's gateway — a "
                    f"foreign listener holds the port",
                )
            print(f"  ✓ gateway serving at {url}")
            return verdict, url
        if verdict is not None:
            # Direct evidence: the gateway answered, with a status no wait improves.
            return verdict, f"HTTP {probe.status} from {url}: {probe.detail}"
        # No usable answer — the expected reading mid-boot. Before spending the bound
        # on it, look for the two states in which the wait could only expire.
        candidate = _infer_hopeless(url, probe)
        if candidate is not None and inferred is not None and candidate[0] is inferred[0]:
            streak += 1
        else:
            streak = 1 if candidate is not None else 0
        inferred = candidate
        if candidate is not None and streak >= _EARLY_EXIT_CONFIRMATIONS:
            return candidate
        if time.monotonic() >= deadline:
            return (
                GatewayReadiness.TIMED_OUT,
                f"no answer at {url} within {timeout_s:.0f}s ({probe.detail})",
            )
        now = time.monotonic()
        if now - noted_at >= _WAIT_NOTE_INTERVAL_S:
            noted_at = now
            print(f"  · not serving yet ({probe.detail}); waiting", file=sys.stderr)
        time.sleep(min(_READY_PROBE_INTERVAL_S, max(0.0, deadline - time.monotonic())))


def _infer_hopeless(url: str, probe: GatewayProbe) -> tuple[GatewayReadiness, str] | None:
    """For a dial that got no answer: is there evidence that waiting cannot help?

    Returns the (verdict, detail) pair to exit on, or None to keep waiting. Kept
    separate from the loop so the two inferences read as what they are — a pair of
    second observations that explain the silence — and so the confirmation rule that
    guards both stays in one place, in the caller.
    """
    if not _gateway_session_alive():
        return (
            GatewayReadiness.GATEWAY_GONE,
            f"the ava-gateway session is not running (last dial: {probe.detail})",
        )
    if _serving_locally():
        return (
            GatewayReadiness.OFF_BOX_UNREACHABLE,
            f"serving on loopback but not at {url} ({probe.detail})",
        )
    return None


def _firewall_attribution() -> str:
    """The second half of the `OFF_BOX_UNREACHABLE` sentence: is the app firewall
    the cause, and if so what fixes it.

    This verdict has exactly two plausible causes — a macOS ALF rule that no longer
    covers the serving binary (issue #949), or an `AVA_GATEWAY_URL` naming an address
    the runners cannot dial — and the old wording named both without choosing, which
    makes an operator check the firewall first every time even on a Linux gateway or
    one with the firewall off. The converge audit answers which, so ask it: it runs
    on this host, needs no privilege, and is the same detector `ava converge` reports
    from, so the rollout report and the converge report can never contradict.

    Deliberately does not fail the sentence if the audit itself raises: the caller is
    already explaining a failure, and an exception here would replace a real
    diagnosis with a stack trace.
    """
    try:
        from cli.commands._converge_firewall import audit_this_host
        from shared.machine import machine_role

        audit = audit_this_host(frozenset(machine_role()))
    except Exception as exc:  # a broken audit must not eat the verdict
        return (
            f"could not audit the host firewall ({exc}); check its allow rule for this "
            f"python binary (a `uv python` bump orphans it — issue #949) and that "
            f"AVA_GATEWAY_URL names an address the runners can dial"
        )
    if audit.needs_operator:
        joined = " && ".join(audit.repair_commands())
        return (
            f"the host firewall IS the cause — {audit.detail}. Run `{joined}`, then "
            f"`ava start` to re-bind (`ava converge` reprints this)"
        )
    return (
        f"the host firewall is NOT the cause ({audit.detail}), so look at the address: "
        f"verify AVA_GATEWAY_URL / AVA_MACHINE_HOST names an interface the runners can "
        f"dial and that it is still assigned"
    )


def gateway_readiness_detail(verdict: GatewayReadiness, detail: str) -> str:
    """The operator-facing sentence for a non-SERVING verdict — what broke and what to
    do about it. One line per verdict because the next move genuinely differs."""
    if verdict is GatewayReadiness.GATEWAY_GONE:
        return (
            f"the gateway is NOT running — {detail}. It came up on the new commit and "
            f"died; read `$AVA_HOME/logs/gateway*.log` (a boot-time schema assertion or "
            f"a port still held by an old process are the usual causes), then `ava start`"
        )
    if verdict is GatewayReadiness.OFF_BOX_UNREACHABLE:
        return (
            f"the gateway is serving but the cluster cannot reach it — {detail}. The "
            f"process is healthy and the network path is not. "
            f"{_firewall_attribution()}"
        )
    if verdict is GatewayReadiness.REFUSED:
        return (
            f"the gateway answered but refused the probe — {detail}. A credential or "
            f"route mismatch, not a timing problem: verify AVA_CLUSTER_SECRET matches "
            f"across the cluster and that this gateway serves {GATEWAY_PROBE_PATH}"
        )
    return (
        f"the gateway never started serving — {detail}. Its session is alive but it has "
        f"bound nothing, so it is hung rather than slow; read "
        f"`$AVA_HOME/logs/gateway*.log`"
    )
