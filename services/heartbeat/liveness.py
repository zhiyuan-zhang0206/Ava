"""Agent liveness pass — gateway-owned derivation of `agents_meta.liveness_state`.

The heartbeat daemon (gateway, one per cluster) runs this pass on a slow cadence.
It combines machine reachability with the process lease so a host that drops
offline cannot leave an agent displayed as online solely because its durable
`agents_meta.status` still reads 'idling' or 'running'.

Two signals, merged per agent:

- **Machine reachability** — each agent-runner is probed via the `status_probe`
  op at its machines-table ops URL (the uniform-RPC path; the local machine is
  dialed at its localhost URL like any other). A machine is judged offline only
  after two *consecutive* failed probes (`_OFFLINE_AFTER_FAILURES`), so one
  dropped packet never flips the fleet; one success resets the count. Results
  land in `machine_probe` (deliberately a separate table — the machines row is
  a recomputed composition of machine_units and any column there would be
  clobbered by register_self).
- **Process lease** — `agents_meta.lease_expires_at` (R1, Task #1021): the
  agent process renews it every 60s while alive, so expiry with the machine up
  means a dead/wedged process. An unclaimed `idling` row has no process yet, so
  it stays
  `unknown` until its atomic claim writes `started_at`.

Per-agent merge (`liveness_state`):

- unclaimed idling -> 'unknown' (no process ownership yet)
- machine offline  -> 'offline' (whole host unreachable)
- running/idling with an expired (or never granted) lease -> 'offline'
- everything else on a reachable machine -> 'online'
- 'terminated' rows are never judged; rows whose machine is not in the
  machines table (or that the gateway has not judged yet) stay 'unknown',
  with no invented successful observation timestamp. The frontend exposes
  observation freshness separately from lifecycle intent.

`status` stays lifecycle intent — the pass never transitions it (R1 invariant
#1). A machine coming back is self-healing: its host resumes pending work
and the next pass re-marks the identities online.

Machine alerting uses a separate episode clock: `machine_probe.transition_since`
is set on the first failed probe and cleared on success. The shared transition
policy stays silent through normal recovery, then fires WARNING and escalates
the same alert instance to ERROR. This pass reads no deploy context, so a
runner offline across an update grades from its true start like any other outage.

The probe path is injectable (`probe` argument) so tests can run the full
DB merge without dialing real ops servers.
"""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.agents.observation.evidence import (
    LIVENESS_PASS_INTERVAL_S,
    MACHINE_OFFLINE_AFTER_FAILURES,
)
from base.cluster.machines import list_agent_runners, list_roster_agent_runners
from base.config import settings
from base.db import Database
from base.db.transaction import write_transaction
from base.deploy.transition import transition_severity
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from ops import cluster_rpc
from ops.cluster_status import ClusterStatus

_log = logging.getLogger("services.heartbeat.liveness")

# Per-machine status_probe timeout — `settings.gateway.status_probe_timeout_seconds`
# (default 8s), the SAME setting the roster's probe reads
# (gateway/cluster/status.py), so the two probes stay aligned by construction
# (task #1200: a 3.0s hardcode here and in the roster flipped a slow-but-healthy
# WSL runner offline — its status_snapshot measured 3.07-3.27s — while a
# genuinely offline host still refuses fast, so the wider budget costs only the
# anti-jitter margin, never the detection latency of a real outage).

# Consecutive failed probes before a machine is judged offline. The pass runs
# once per minute, so this is a ~2-minute anti-jitter window (a single dropped
# packet or a mid-restart runner is not "offline").
_OFFLINE_AFTER_FAILURES = MACHINE_OFFLINE_AFTER_FAILURES

# How often the pass runs. Independent of the check-in dispatch step (15s).
_PASS_INTERVAL_S = LIVENESS_PASS_INTERVAL_S


@dataclass(frozen=True)
class ProbeOutcome:
    """What one status_probe round-trip told: whether the ops server answered, the
    agent-host verdict, and the validated ClusterStatus payload (None when the host
    did not answer or its body is not a ClusterStatus)."""

    reached: bool
    host_online: bool | None
    status: dict[str, Any] | None


_UNREACHED = ProbeOutcome(reached=False, host_online=None, status=None)


async def _probe_machine(name: str, probe: Callable[..., Awaitable[object]]) -> ProbeOutcome:
    """One status_probe round-trip.

    An unreachable host or a failed op (timeout and transport errors surface as
    unreachable) is a probe failure — the caller counts consecutive failures.
    Anything else is a bug and propagates.
    """
    try:
        result = await probe(
            target_machine=name,
            kind="status_probe",
            payload={},
            timeout_s=settings.gateway.status_probe_timeout_seconds,
            # No transport retry: an offline host is steady-state and the
            # pass has its own consecutive-failure gate; retrying just stalls
            # the fan-out (same reasoning as the roster probe).
            retries=0,
        )
        try:
            status = ClusterStatus.model_validate(result)
        except ValueError:
            # A reachable old/malformed ops server is not evidence of a host.
            return ProbeOutcome(reached=True, host_online=None, status=None)
        return ProbeOutcome(
            reached=True,
            host_online=status.agent_host_online if status.machine_name == name else None,
            status=status.model_dump(mode="json"),
        )
    except (cluster_rpc.ClusterOpUnreachable, cluster_rpc.ClusterOpFailed):
        return _UNREACHED


def _fire_machine_offline(
    conn: Any,
    name: str,
    *,
    new_cf: int,
    transition_since: datetime | None,
    now: datetime,
) -> None:
    """Fire (or escalate) the open "machine offline" alert for a failed probe."""
    from base.telemetry.alerts import (
        display_language,
        fingerprint,
        notify_im,
        notify_text,
        stamp_notified,
        upsert_alert,
    )

    identity_labels = {"alertname": "machine offline", "machine": name}
    stable_fp = fingerprint(identity_labels)
    assert transition_since is not None  # noqa: S101 — every failed probe persists it
    severity = transition_severity(
        transition_since,
        now,
        warning_after_s=settings.alerts.transition_warning_seconds,
        error_after_s=settings.alerts.transition_error_seconds,
    )
    if severity is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT starts_at, severity, notified_at FROM alerts "
            "WHERE labels->>'alertname' = 'machine offline' "
            "AND labels->>'machine' = %s AND status = 'unresolved' "
            "ORDER BY starts_at DESC LIMIT 1",
            (name,),
        )
        open_row = cur.fetchone()
    if open_row is not None and open_row[1] == severity and open_row[2] is not None:
        return
    starts_at = open_row[0] if open_row is not None else transition_since
    labels = {**identity_labels, "severity": severity}
    elapsed_minutes = max(0.0, (now - transition_since).total_seconds()) / 60.0
    alert = {
        "status": "firing",
        "labels": labels,
        "annotations": {
            "summary": (
                f"machine {name} offline for {elapsed_minutes:.1f} minutes: "
                f"{new_cf} consecutive failed probes"
            )
        },
        "starts_at": starts_at.isoformat(),
        "fingerprint": stable_fp,
    }
    key, _did_insert, should_notify, _row = upsert_alert(conn, alert, source="machine-probe")
    lang = display_language(conn)
    if should_notify and notify_im(notify_text(alert, lang)):
        stamp_notified(conn, [key])


def _resolve_machine_offline(conn: Any, name: str, *, now: datetime) -> None:
    """Resolve every open "machine offline" alert of a machine that is back online."""
    from base.telemetry.alerts import (
        display_language,
        notify_im,
        notify_text,
        stamp_notified,
        upsert_alert,
    )

    identity_labels = {"alertname": "machine offline", "machine": name}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT starts_at, fingerprint, severity FROM alerts "
            "WHERE labels->>'alertname' = 'machine offline' "
            "AND labels->>'machine' = %s AND status = 'unresolved' "
            "ORDER BY starts_at DESC",
            (name,),
        )
        open_rows = cur.fetchall()
    if not open_rows:
        return
    lang = display_language(conn)
    for starts_at, fp, severity in open_rows:
        alert = {
            "status": "resolved",
            "labels": {**identity_labels, "severity": severity},
            "annotations": {"summary": f"machine {name} back online"},
            "starts_at": starts_at.isoformat(),
            "ends_at": now.isoformat(),
            "fingerprint": fp,
        }
        key, _did_insert, should_notify, _row = upsert_alert(conn, alert, source="machine-probe")
        if should_notify and notify_im(notify_text(alert, lang)):
            stamp_notified(conn, [key])


def _machine_alert_edges(
    conn: Any,
    name: str,
    *,
    ok: bool,
    old_online: bool | None,
    new_cf: int,
    transition_since: datetime | None,
    now: datetime,
) -> None:
    """Grade one machine transition and persist its firing/recovery edges.

    Direct write, ``source="machine-probe"`` — the liveness pass runs on the
    gateway with the DB at hand. The stable fingerprint excludes severity, so
    WARNING -> ERROR updates one instance and the shared notification gate
    treats the increase as a new firing transition. Open rows are discovered
    by stable identity labels so rows written before that convention still
    recover.

    Best-effort: alerting is a side channel and must never break the pass
    (DB errors propagate to the caller's per-pass catch, IM errors are
    swallowed by ``notify_im``).
    """
    if not ok:
        _fire_machine_offline(conn, name, new_cf=new_cf, transition_since=transition_since, now=now)
    elif old_online is False:
        _resolve_machine_offline(conn, name, now=now)


async def _record_probe(
    pool: ConnectionPool, name: str, *, ok: bool, host_online: bool | None
) -> None:
    """UPSERT one probe outcome into machine_probe, bumping the consecutive
    failure count on failure and resetting it on success — and record the
    offline/online edge as an alerts row (see ``_machine_alert_edges``)."""
    with write_transaction(pool) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT online, consecutive_failures FROM machine_probe WHERE machine_name = %s",
                (name,),
            )
            old = cur.fetchone()
        old_online: bool | None = old[0] if old else None
        new_cf = 0 if ok else (1 if old is None else old[1] + 1)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO machine_probe "
                "(machine_name, online, agent_host_online, consecutive_failures, last_probe_at, transition_since) "
                "VALUES (%s, %s, %s, %s, now(), CASE WHEN %s THEN NULL ELSE now() END) "
                "ON CONFLICT (machine_name) DO UPDATE SET "
                "  online = EXCLUDED.online, "
                "  agent_host_online = EXCLUDED.agent_host_online, "
                "  consecutive_failures = EXCLUDED.consecutive_failures, "
                "  last_probe_at = now(), "
                "  transition_since = CASE WHEN EXCLUDED.online THEN NULL "
                "    ELSE COALESCE(machine_probe.transition_since, EXCLUDED.transition_since) END "
                "RETURNING transition_since, last_probe_at",
                (name, ok, host_online, new_cf, ok),
            )
            probe_row = cast("tuple[datetime | None, datetime] | None", cur.fetchone())
            assert probe_row is not None  # noqa: S101 — UPSERT RETURNING always yields one row
            transition_since, now = probe_row
        _machine_alert_edges(
            conn,
            name,
            ok=ok,
            old_online=old_online,
            new_cf=new_cf,
            transition_since=transition_since,
            now=now,
        )


def _merge_liveness(pool: ConnectionPool) -> list[int]:
    """Recompute `liveness_state` for every non-terminated row whose machine is
    registered, from the current machine_probe rows and lease state. Return the
    ids whose user-visible liveness crossed into or out of `offline`.

    Pure SQL so the merge is one statement, atomic and O(agents) — the same
    shape the reaper's passes use. A machine with no probe row yet (never
    probed) reads as reachable (`cf = 0`), which matches the fresh-cluster
    behaviour: rows start 'unknown' and only ever flip to offline on real
    probe failures / lease expiry.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "WITH probe AS ("
            "  SELECT m.name AS machine_name,"
            "         mp.consecutive_failures AS cf, mp.last_probe_at AS observed_at"
            "  FROM machines m"
            "  LEFT JOIN machine_probe mp ON mp.machine_name = m.name"
            "), desired AS ("
            "  SELECT a.id, a.liveness_state AS old_liveness_state,"
            "    CASE "
            "      WHEN p.observed_at IS NULL THEN 'unknown' "
            "      WHEN a.status = 'idling' AND a.started_at IS NULL THEN 'unknown' "
            "      WHEN p.cf >= %s THEN 'offline' "
            "      WHEN a.status IN ('running', 'idling') AND a.started_at IS NOT NULL "
            "           AND (a.lease_expires_at IS NULL OR a.lease_expires_at <= now()) "
            "        THEN 'offline' "
            "      ELSE 'online' "
            "    END AS new_liveness_state, p.observed_at "
            "  FROM agents_meta a "
            "  JOIN probe p ON a.machine = p.machine_name "
            "  WHERE a.status != 'terminated'"
            ") "
            "UPDATE agents_meta a "
            "SET liveness_state = d.new_liveness_state, "
            "    last_probe_at = d.observed_at "
            "FROM desired d "
            "WHERE a.id = d.id "
            "RETURNING a.id, d.old_liveness_state, d.new_liveness_state",
            (_OFFLINE_AFTER_FAILURES,),
        )
        rows = cur.fetchall()
    # `unknown` is already rendered conservatively as online, so the first
    # judgement unknown -> online is not a user-visible edge and must not emit
    # a fleet-sized startup burst. Broadcast only edges entering/leaving the
    # offline state; one snapshot per changed agent lets the existing R4 fold
    # update mounted clients without inventing a second liveness transport.
    return [
        int(agent_id)
        for agent_id, old_state, new_state in rows
        if old_state != new_state and (old_state == "offline" or new_state == "offline")
    ]


async def _record_snapshot(pool: ConnectionPool, name: str, outcome: ProbeOutcome) -> None:
    """UPSERT the roster's read-model row for one machine: the latest attempt, and
    the last ClusterStatus kept across a failed attempt (a reachable answer that is
    not a ClusterStatus clears it, so the roster shows reached-but-unknown)."""
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machine_status_snapshot "
            "(machine_name, observed_at, reachable, consecutive_failures, status, status_at) "
            "VALUES (%s, now(), %s, CASE WHEN %s THEN 0 ELSE 1 END, %s, "
            "        CASE WHEN %s THEN now() END) "
            "ON CONFLICT (machine_name) DO UPDATE SET "
            "  observed_at = now(), "
            "  reachable = EXCLUDED.reachable, "
            "  consecutive_failures = CASE WHEN EXCLUDED.reachable THEN 0 "
            "    ELSE machine_status_snapshot.consecutive_failures + 1 END, "
            "  status = CASE WHEN EXCLUDED.reachable THEN EXCLUDED.status "
            "    ELSE machine_status_snapshot.status END, "
            "  status_at = CASE WHEN EXCLUDED.reachable THEN EXCLUDED.status_at "
            "    ELSE machine_status_snapshot.status_at END",
            (
                name,
                outcome.reached,
                outcome.reached,
                None if outcome.status is None else Jsonb(outcome.status),
                outcome.status is not None,
            ),
        )


async def run_liveness_pass(
    db: Database,
    pool: ConnectionPool,
    bus: EventBus,
    probe: Callable[..., Awaitable[object]] | None = None,
) -> None:
    """One liveness pass: probe every roster-visible agent-runner once, record the
    outcome of the rollout targets as agent-liveness state, snapshot every probed
    machine for the roster read, then merge.

    `probe` is injectable for tests (default: the real cluster RPC resolving addresses through
    `db`). Probe
    failures are per-machine and quiet — a down host is steady-state; the
    pass keeps running for the hosts that are up.
    """
    probe = probe if probe is not None else functools.partial(cluster_rpc.dispatch_to_machine, db)
    targets = {name for name, _url in list_agent_runners(db)}
    machines = sorted(targets | {name for name, _url in list_roster_agent_runners(db)})
    if not machines:
        return
    results = await asyncio.gather(*(_probe_machine(name, probe) for name in machines))
    outcomes = dict(zip(machines, results, strict=True))
    for name in machines:
        outcome = outcomes[name]
        if name in targets:
            await _record_probe(pool, name, ok=outcome.reached, host_online=outcome.host_online)
        await _record_snapshot(pool, name, outcome)
    changed_agent_ids = _merge_liveness(pool)
    # `_merge_liveness` committed before these best-effort invalidation hints.
    for agent_id in changed_agent_ids:
        publish_agent_updated_sync(bus, agent_id)
    _log.info(
        "[heartbeat] liveness pass: %d machines probed (%d reachable), agents_meta merged",
        len(machines),
        sum(outcome.reached for outcome in results),
    )
