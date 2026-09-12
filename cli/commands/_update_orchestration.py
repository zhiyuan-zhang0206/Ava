"""Gateway rollout orchestration helpers — the discrete steps
`cli/commands/update.py:_run_gateway_orchestration_inner` composes.

Split out of `update.py` (which holds the orchestration itself) to keep that file
under the size ceiling; these are the standalone steps:
- `_classify_rollout` — decide what kind of change is imminent + whether to
  fast-path (docs-only / frontend-only) out of the full pause/migrate/fan-out.
- `_persist_cluster_pin` — record the cluster's standing `cluster_target_sha`
  after the gateway reaches the rollout target, and stage backend changes for
  health-probe observation before they advance `last_known_good_sha`.
- `_resolve_fanout_targets` — the fan-out target list, reconciled against a live
  probe of every host the `stopped_at` filter would drop, and reported as
  "N of M registered agent-runner(s)".
- `_phase_b_targets` — that list minus this host, because a co-located
  gateway,agent-runner box updates itself in the local leg and must not also be
  handed a self-update by its own fan-out.

`_changed_paths_vs_origin` / `_run_frontend_only_update` are looked up through the
`cli.commands` namespace (so tests can monkeypatch them) rather than imported here.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cli.commands._update_git import GitPullFailed
from shared.api_contracts.status import MachineStatus
from shared.repo_change import classify_change as _classify_change


@dataclass(frozen=True)
class _BaselineValue[T]:
    value: T | None = None
    reason: str | None = None


def _baseline_read[T](read: Callable[[], T | None]) -> _BaselineValue[T]:
    """Keep a failed observation local to its source; never abort the rollout."""
    try:
        return _BaselineValue(read())
    except Exception as exc:  # fail-fast-ok: independent best-effort observations
        return _BaselineValue(reason=f"{type(exc).__name__}: {exc}")


@dataclass(frozen=True)
class _HealthProbeBaseline:
    failures: _BaselineValue[tuple[int, str, str, str]]
    pending_lkg: _BaselineValue[tuple[str, int]]
    alert: _BaselineValue[str]


@dataclass(frozen=True)
class _HealthBaseline:
    captured_at: datetime
    target_sha: str | None
    pin: _BaselineValue[tuple[str | None, str | None, tuple[str, datetime] | None]]
    machines: _BaselineValue[list[MachineStatus]]
    postures: _BaselineValue[dict[str, str]]
    health_probe: _HealthProbeBaseline
    db_size: _BaselineValue[int]


def _baseline_file(name: str) -> list[str] | None:
    from shared.paths import ava_home

    try:
        return (ava_home() / name).read_text().splitlines()
    except FileNotFoundError:
        return None


def _baseline_failures() -> tuple[int, str, str, str] | None:
    from cli.commands._health_alerts import FAILURE_COUNT_FILE

    lines = _baseline_file(FAILURE_COUNT_FILE)
    if lines is None:
        return None
    count, failure_class, reason, timestamp = lines
    parsed_count = int(count)
    if parsed_count < 0:
        raise ValueError("negative failure count")
    datetime.fromisoformat(timestamp)
    return parsed_count, failure_class, reason, timestamp


def _baseline_pending_lkg() -> tuple[str, int] | None:
    from cli.commands._cluster_health import PENDING_LKG_PASSES_FILE

    lines = _baseline_file(PENDING_LKG_PASSES_FILE)
    if lines is None:
        return None
    sha, count = lines
    parsed_count = int(count)
    if not sha or parsed_count < 0:
        raise ValueError("invalid pending-known-good state")
    return sha, parsed_count


def _baseline_alert() -> str | None:
    from cli.commands._health_alerts import ALERT_STATE_FILE

    lines = _baseline_file(ALERT_STATE_FILE)
    return None if lines is None else lines[0]


def _collect_health_baseline(*, target_sha: str | None) -> _HealthBaseline:
    """Read pre-rollout evidence, independently degrading each unavailable source.

    No health probe is run and no state is written. Missing files mean no stored
    observation; malformed files remain distinguishable from healthy state.
    """

    def pin() -> tuple[str | None, str | None, tuple[str, datetime] | None]:
        from shared.cluster_pin import (
            get_cluster_target_sha,
            get_last_known_good_sha,
            get_pending_known_good,
        )

        return get_cluster_target_sha(), get_last_known_good_sha(), get_pending_known_good()

    def machines() -> list[MachineStatus]:
        from shared.http_dial import get as dial_get
        from shared.machine import gateway_api_base, gateway_auth_headers

        resp = dial_get(
            f"{gateway_api_base()}/api/cluster/roster",
            timeout=10.0,
            headers=gateway_auth_headers(),
        )
        resp.raise_for_status()
        return [MachineStatus.model_validate(machine) for machine in resp.json()]

    def postures() -> dict[str, str]:
        from shared.host_deploy_state import read_all

        return {name: state.posture for name, state in read_all().items()}

    def db_size() -> int:
        from shared.db import connect

        with connect(autocommit=True) as conn:
            row = conn.execute("SELECT pg_database_size(current_database())").fetchone()
        if row is None:
            raise ValueError("database size query returned no row")
        return int(row[0])

    return _HealthBaseline(
        captured_at=datetime.now(UTC),
        target_sha=target_sha,
        pin=_baseline_read(pin),
        machines=_baseline_read(machines),
        postures=_baseline_read(postures),
        health_probe=_HealthProbeBaseline(
            failures=_baseline_read(_baseline_failures),
            pending_lkg=_baseline_read(_baseline_pending_lkg),
            alert=_baseline_read(_baseline_alert),
        ),
        db_size=_baseline_read(db_size),
    )


def _baseline_absent(result: _BaselineValue[object]) -> str:
    if result.reason is None:
        return "(none)"
    # Source messages can contain newlines; retain one stable log line per field.
    return f"(unavailable: {' '.join(result.reason.splitlines())})"


def _format_health_baseline(baseline: _HealthBaseline) -> list[str]:
    """Render a snapshot without I/O or changes to health/rollback decisions."""
    from cli.commands._cluster_health import PENDING_LKG_PASSES
    from shared.machine import format_capabilities

    target = baseline.target_sha[:7] if baseline.target_sha else "restart-only"
    lines = [
        f"── pre-rollout health baseline ({baseline.captured_at.isoformat()}; target={target}) ──"
    ]
    pin = _baseline_absent(baseline.pin)
    if baseline.pin.value is not None:
        current, known_good, pending = baseline.pin.value
        pending_text = f"{pending[0][:7]} (at {pending[1].isoformat()})" if pending else "(none)"
        pin = (
            f"target={current[:7] if current else '(none)'} "
            f"last-known-good={known_good[:7] if known_good else '(none)'} pending={pending_text}"
        )
    lines.append(f"cluster pin: {pin}")
    if baseline.machines.value is None:
        lines.append(f"machines: {_baseline_absent(baseline.machines)}")
    else:
        lines.append("machines:" if baseline.machines.value else "machines: (none)")
        for machine in sorted(baseline.machines.value, key=lambda machine: machine.name):
            posture = _baseline_absent(baseline.postures)
            if baseline.postures.value is not None:
                posture = (
                    baseline.postures.value[machine.name]  # noqa: SIM401 - missing rows are expected
                    if machine.name in baseline.postures.value
                    else "(no row)"
                )
            role = format_capabilities(
                machine.serve_gateway,
                machine.serve_agent_runner,
                machine.serve_observability_station,
            )
            on_pin = "(unknown)"
            if machine.on_pin is not None and machine.head_sha is not None:
                on_pin = f"{'✓' if machine.on_pin else '✗'} {machine.head_sha[:7]}"
            status = "online" if machine.online else "stopped" if machine.stopped_at else "offline"
            paused = "?" if machine.paused is None else "yes" if machine.paused else "no"
            staging = " (staging)" if machine.is_staging else ""
            lines.append(
                f"  {machine.name}{staging}  {role}  posture={posture}  on-pin={on_pin}  "
                f"status={status}  paused={paused}"
            )
    probe = baseline.health_probe
    failures = _baseline_absent(probe.failures)
    if probe.failures.value is not None:
        count, failure_class, reason, timestamp = probe.failures.value
        failures = f"{count}/3 ({failure_class}, recorded-at {timestamp}, reason={reason!r})"
    pending_lkg = _baseline_absent(probe.pending_lkg)
    if probe.pending_lkg.value is not None:
        sha, count = probe.pending_lkg.value
        pending_lkg = f"{count}/{PENDING_LKG_PASSES} ({sha[:7]})"
    alert = (
        repr(probe.alert.value) if probe.alert.value is not None else _baseline_absent(probe.alert)
    )
    lines.append(
        f"health-probe (this host): failures={failures}; pending-lkg={pending_lkg}; alert-episode={alert}"
    )
    size = _baseline_absent(baseline.db_size)
    if baseline.db_size.value is not None:
        amount = float(baseline.db_size.value)
        unit = "B"
        for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
            if amount < 1024 or unit == "PiB":
                break
            amount /= 1024
        size = f"size={amount:.1f} {unit} ({baseline.db_size.value} bytes)"
    lines.extend((f"db: {size}", "── end pre-rollout health baseline ──"))
    return lines


def _record_health_baseline(*, target_sha: str | None) -> None:
    """Append best-effort evidence to the rollout log; never abort a rollout."""
    try:
        baseline = _collect_health_baseline(target_sha=target_sha)
        print("\n".join(_format_health_baseline(baseline)))
    except Exception as exc:  # fail-fast-ok: observability must not abort a rollout
        reason = " ".join(f"{type(exc).__name__}: {exc}".splitlines())
        print(f"⚠ pre-rollout health baseline failed: {reason}", file=sys.stderr)


def _persist_cluster_pin(target_sha: str, *, origin: str, advance_known_good: bool = False) -> None:
    """Record `target_sha` as the cluster's standing pin (`cluster_target_sha`)
    once the gateway's local update reaches it.

    When `advance_known_good=True` (a backend-changing rollout), the target is
    recorded as pending-known-good. The health probe promotes it only after its
    observation window, preserving the prior release as the automatic rollback
    anchor. Frontend-only / docs-only / restart-only rollouts pass
    `advance_known_good=False` (no code change, so the known-good anchor stays
    on the last real code change).

    Agent-runners converge to `target_sha` in Phase B; `ava status` compares each
    node's HEAD against it. `origin` (who triggered the rollout) rides into the
    pin's `updated_by` alongside the executing process, so the pin row alone
    answers "who moved the cluster"."""
    from shared.cluster_pin import (
        get_last_known_good_sha,
        set_cluster_target_sha,
        set_target_with_pending_known_good,
    )
    from shared.machine import machine_name

    set_by = f"{machine_name()}:pid{os.getpid()} origin={origin}"
    if advance_known_good:
        old_lkg = get_last_known_good_sha()
        old_display = old_lkg[:7] if old_lkg else "(none)"
        set_target_with_pending_known_good(target_sha, set_by=set_by)
        print(
            f"  ✓ cluster pin updated -> {target_sha[:7]} "
            f"(last-known-good NOT advanced: {old_display} pending for the observation window, "
            f"origin={origin})"
        )
    else:
        set_cluster_target_sha(target_sha, set_by=set_by)
        print(f"  ✓ cluster pin updated -> {target_sha[:7]} (origin={origin})")


def _classify_rollout(repo: Path, *, restart_only: bool, origin: str) -> tuple[int | None, bool]:
    """Classify the imminent change for the gateway orchestration.

    Returns `(early_rc, restart_frontend)`: when `early_rc` is not None the
    orchestration should return it immediately — a fast path that skips the full
    pause/quiesce/migrate/fan-out (docs-only → pull only → 0; frontend-only →
    rebuild ava-frontend → its rc; a classify/pull failure → 1). Otherwise
    `restart_frontend` says whether the full orchestration rebuilds ava-frontend
    (only when the frontend changed too). `restart_only` skips classification and
    always bounces (rebuild frontend; no pull)."""
    import cli.commands as _ns

    if restart_only:
        # A restart always bounces; rebuild the frontend too (a config change can
        # affect the UI process).
        print("\n→ cluster restart (no pull): bounce every service on current code")
        return None, True
    from shared.running_sha import get as get_running_sha
    from shared.source_integrity import get as get_installed_sha
    from shared.source_integrity import installed_sha_needs_replay

    installed_sha = get_installed_sha()
    running_sha = get_running_sha()
    if installed_sha_needs_replay(installed_sha, running_sha, repo=repo):
        print(
            "\n→ half-deployed state: installed commit is ahead of the running commit; "
            "replaying the full rollout"
        )
        return None, True
    # Classify before touching anything. On a git error, fall back to a full
    # restart (restart_frontend=True) — never under-restart on a fetch hiccup.
    try:
        paths = _ns._changed_paths_vs_origin()
    except GitPullFailed as e:
        print(f"  ⚠ could not classify change ({e}); doing a full restart", file=sys.stderr)
        return None, True
    frontend, backend = _classify_change(paths)
    if not frontend and not backend:
        print("\n→ no code change (docs-only / already up to date) — pull only, no restart")
        print("\n→ git pull origin main")
        try:
            pull = _ns.git_pull_main()
        except Exception as e:
            print(f"  ✗ {e}", file=sys.stderr)
            return 1, True
        # The pull moved HEAD; advance the standing pin with it (same reason as
        # the frontend-only fast path: an un-advanced pin reads as off-pin).
        # Docs-only: no code changed, so do NOT advance last_known_good.
        import shared.running_sha as _rsha

        _rsha.set(pull.to_sha)
        _persist_cluster_pin(pull.to_sha, origin=origin, advance_known_good=False)
        return 0, True
    if frontend and not backend:
        return _ns._run_frontend_only_update(repo, origin), True
    return None, frontend  # backend changed; rebuild frontend only if it changed too


# One short status_probe per stop-marked host. Bounded and parallel: this runs
# before Phase A, so it delays every rollout by at most this much even when a
# decommissioned host is in the table forever.
_STOPPED_PROBE_TIMEOUT_S = 5.0


async def _probe_stopped_agent_runners(
    stopped: list[tuple[str, str | None]],
) -> dict[str, str]:
    """status_probe each stop-marked agent-runner; return name -> verdict.

    Verdicts: `'live'` (answered, and answered under its own name — the marker is
    stale), `'mismatch'` (answered under a DIFFERENT machine_name, so this row's
    dial URL points at the wrong host), `'down'` (unreachable / the op failed —
    the marker agrees with reality).
    """
    from ops import cluster_rpc as cr

    async def _one(name: str, url: str | None) -> tuple[str, str]:
        try:
            result = await cr.dispatch_to_machine(
                target_machine=name,
                kind="status_probe",
                payload={},
                timeout_s=_STOPPED_PROBE_TIMEOUT_S,
                ops_url=url,
            )
        except (cr.ClusterOpUnreachable, cr.ClusterOpFailed):
            return name, "down"
        if isinstance(result, dict) and result.get("machine_name") not in (None, name):
            return name, "mismatch"
        return name, "live"

    return dict(await asyncio.gather(*[_one(name, url) for name, url in stopped]))


def _resolve_fanout_targets(*, clear_stale_markers: bool = True) -> list[tuple[str, str | None]]:
    """The rollout's agent-runner fan-out list, reconciled against a live probe of
    every host the `stopped_at` filter would drop — and reported as N of M.

    Two sources of truth used to disagree in silence. `list_agent_runners()`
    filters on `machines.stopped_at`, while the roster's `online` column means "a
    live probe answered"; a host carrying a stale stop latch therefore read
    `online` on every dashboard while sitting outside the fan-out entirely, and
    the rollout announced a bare "3" where a reader of the roster expects 4 (the
    2026-07-28 rollout on a runner). That rollout carried no migrations so the
    excluded host was harmless — a host outside the quiesce during a
    migration-carrying rollout is a host writing the central DB on old code.

    So the marker is not trusted on its own: every excluded host is probed, and
    one that answers under its own name is **re-included** in the rollout (the
    probe is the arbiter — a genuinely stopped host cannot answer) and has its
    stale composed-row marker cleared, so the roster stops contradicting the
    fan-out. A host that answers under a different name is left out and reported
    loudly: its row's dial URL points at the wrong host, which re-including would
    turn into an update fired at a stranger.

    The count line is printed unconditionally, including the all-clear case: a
    silent count is what made the exclusion invisible.
    """
    import cli.commands as _ns
    import shared.machines

    targets = _ns._list_agent_runners()
    stopped = shared.machines.list_stopped_agent_runners()
    known = len(targets) + len(stopped)
    if not stopped:
        print(f"\n→ rollout targets: {len(targets)} of {known} registered agent-runner(s)")
        return targets

    print(
        f"\n→ reconciling {len(stopped)} agent-runner(s) marked stopped against a live probe: "
        f"{', '.join(name for name, _ in stopped)}"
    )
    verdicts = asyncio.run(_probe_stopped_agent_runners(stopped))
    reconciled = list(targets)
    for name, url in stopped:
        verdict = verdicts[name]
        if verdict != "live":
            from shared.db import connect

            with connect() as conn:
                unsafe = conn.execute(
                    "SELECT 1 FROM agents_meta WHERE machine=%s AND status<>'terminated' "
                    "AND (runtime_owner IS NOT NULL OR pid IS NOT NULL "
                    "OR lifecycle_command_id IS NOT NULL OR incarnation_resources IS NOT NULL) LIMIT 1",
                    (name,),
                ).fetchone()
            if unsafe is not None:
                raise RuntimeError(
                    f"stopped marker is not execution proof for {name}; native runtime intent "
                    "is unresolved and the runner cannot confirm drain"
                )
        if verdict == "live":
            print(
                f"  ⚠ {name}: marked stopped in `machines` but its ops server ANSWERED — "
                f"the stop marker is stale (nothing clears it but a completed `ava start`). "
                f"Including it in this rollout; a live host left outside the quiesce writes "
                f"the DB on old code across the migration.",
                file=sys.stderr,
            )
            reconciled.append((name, url))
            if clear_stale_markers:
                _clear_stale_stop_marker(name)
        elif verdict == "mismatch":
            print(
                f"  ✗ {name}: marked stopped, and the probe at its registered URL answered "
                f"under a DIFFERENT machine name — this row's dial URL points at the wrong "
                f"host. NOT included in the rollout; fix the registration "
                f"(`ava cluster status` shows it as MISMATCH).",
                file=sys.stderr,
            )
        else:
            print(f"  · {name}: stopped and unreachable with no admitted native runtime — excluded")
    reconciled.sort()
    print(
        f"\n→ rollout targets: {len(reconciled)} of {known} registered agent-runner(s)"
        f" ({known - len(reconciled)} skipped)"
    )
    return reconciled


def _clear_stale_stop_marker(name: str) -> None:
    """Clear `name`'s stale composed-row stop marker; never raises.

    Best-effort by design — the reconcile above has already put the host back in
    the rollout, which is the part that protects the migration. Failing to tidy
    the roster must not abort a rollout that is otherwise ready to run.
    """
    import shared.machines

    try:
        if shared.machines.clear_stopped_marker(name):
            print(f"    ✓ cleared {name}'s stale stop marker (the probe proves it is live)")
    except Exception as exc:
        print(f"    ⚠ could not clear {name}'s stale stop marker: {exc}", file=sys.stderr)


def _phase_b_targets(
    agent_runners: list[tuple[str, str | None]],
) -> list[tuple[str, str | None]]:
    """The rollout targets **this host must not send a self-update to**: everything in
    `agent_runners` except itself.

    A single box carries `gateway,agent-runner`, so it is a legitimate row in
    `machines.list_agent_runners()` and belongs in every other phase of its own
    rollout — Phase 0's fetch and Phase A's pause are idempotent, and including it
    keeps one code path for single-box and split alike. Phase B is the one phase where
    that stops being true, because its op is not idempotent with what the local leg
    already did: the local leg checked this host out, synced it, migrated and
    `ava start`-ed it, so the fan-out's `cluster_update` is pure redundancy — and it is
    redundancy whose *first* act is killing the ava-gateway session.

    That is the 2026-08-01 rollout (issue #1151). The order it produced was: the
    readiness gate confirms the gateway is serving -> Phase B fans out -> this host's
    own leg kills the gateway it just brought up -> the remote runners' preflight
    probes, dialing that same gateway a second later, take ECONNREFUSED and decline
    (`RESTART_DECLINED_EXIT_CODE`) -> both are reported STALLED and converge only when
    the settle lease lapses ~15 minutes later. The hole was ~9 s. Nothing downstream
    could have prevented it: the gate had already passed, correctly, on a gateway that
    was serving at the moment it was asked.

    So the fix is here rather than in a longer wait somewhere else — the redundant leg
    has no purpose to preserve, and removing it removes the hole instead of making it
    survivable. The runner-side preflight budget added alongside
    (`cli.commands._repo._probe_gateway_or_die`) is defense in depth for holes this
    rollout did not open, not a second half of this fix.

    Split deployments are unaffected: a gateway-only unit does not carry
    `agent-runner`, so it was never in this list to begin with.
    """
    from shared.machine import machine_name

    me = machine_name()
    targets = [(name, url) for name, url in agent_runners if name != me]
    if len(targets) != len(agent_runners):
        print(
            f"  · excluding {me} from Phase B: it is this rollout's orchestrator and the "
            f"local leg above already checked it out, migrated it and restarted it"
        )
    return targets
