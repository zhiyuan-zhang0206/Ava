"""Agent liveness pass — gateway-owned derivation of `agents_meta.liveness_state`.

The heartbeat daemon (gateway, one per cluster) runs this pass on a slow cadence.
It closes the gap behind Task #1174: every corpse detector that reads the
process lease (`ops.controllers.respawn` reaper, wedged, revive) is
machine-scoped — it runs on the agent's own host, so when that host drops
offline (network partition / power-off) nobody reads the lease, and
`agents_meta.status` sits at 'idling'/'running' while the frontend shows a dead
agent as online.

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
  `unknown` until its atomic claim writes `started_at`; `restarting` judges on
  machine reachability alone.

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
the same alert instance to ERROR. A live cluster deploy or this host's updater
lease explains the bounded window without resetting the clock; unreadable
deploy context explains nothing.

The probe path is injectable (`probe` argument) so tests can run the full
DB merge without dialing real ops servers.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, cast

from psycopg_pool import ConnectionPool

from ops import cluster_rpc
from shared import cluster_lock, host_deploy_state
from shared.agent_observation import LIVENESS_PASS_INTERVAL_S, MACHINE_OFFLINE_AFTER_FAILURES
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.live_announce import publish_agent_updated_sync
from shared.machines import list_agent_runners
from shared.transition import transition_severity

_log = logging.getLogger("services.heartbeat.liveness")

# Per-machine status_probe timeout — `settings.gateway.status_probe_timeout_seconds`
# (default 8s), the SAME setting the roster's probe reads
# (gateway/routers/status.py), so the two probes stay aligned by construction
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


async def _probe_machine(
    name: str,
    probe: Callable[..., Awaitable[object]] = cluster_rpc.dispatch_to_machine,
) -> bool:
    """One status_probe round-trip to an agent-runner; True = reachable.

    Any failure (unreachable, op failure, timeout, transport error) is a probe
    failure — the caller counts consecutive failures.
    """
    try:
        await probe(
            target_machine=name,
            kind="status_probe",
            payload={},
            timeout_s=settings.gateway.status_probe_timeout_seconds,
            # No transport retry: an offline host is steady-state and the
            # pass has its own consecutive-failure gate; retrying just stalls
            # the fan-out (same reasoning as the roster probe).
            retries=0,
        )
        return True
    except Exception:
        return False


def _machine_alert_edges(
    conn: Any,
    name: str,
    *,
    ok: bool,
    old_online: bool | None,
    new_cf: int,
    transition_since: datetime | None,
    now: datetime,
    deploy_explains: bool,
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
    from shared.alerts import (
        display_language,
        fingerprint,
        notify_im,
        notify_text,
        stamp_notified,
        upsert_alert,
    )

    identity_labels = {"alertname": "machine offline", "machine": name}
    stable_fp = fingerprint(identity_labels)

    if not ok:
        assert transition_since is not None  # noqa: S101 — every failed probe persists it
        severity = transition_severity(
            transition_since,
            now,
            deploy_explains=deploy_explains,
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
        return

    if ok and old_online is False:
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
            key, _did_insert, should_notify, _row = upsert_alert(
                conn, alert, source="machine-probe"
            )
            if should_notify and notify_im(notify_text(alert, lang)):
                stamp_notified(conn, [key])


async def _record_probe(
    pool: ConnectionPool, name: str, *, ok: bool, deploy_explains: bool
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
                "(machine_name, online, consecutive_failures, last_probe_at, transition_since) "
                "VALUES (%s, %s, %s, now(), CASE WHEN %s THEN NULL ELSE now() END) "
                "ON CONFLICT (machine_name) DO UPDATE SET "
                "  online = EXCLUDED.online, "
                "  consecutive_failures = EXCLUDED.consecutive_failures, "
                "  last_probe_at = now(), "
                "  transition_since = CASE WHEN EXCLUDED.online THEN NULL "
                "    ELSE COALESCE(machine_probe.transition_since, EXCLUDED.transition_since) END "
                "RETURNING transition_since, last_probe_at",
                (name, ok, new_cf, ok),
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
            deploy_explains=deploy_explains,
        )


def _deploy_explanations(names: list[str]) -> dict[str, bool]:
    """Read the pass's deploy context once; unreadable context explains nothing."""
    try:
        cluster_deploy_live = cluster_lock.read_update_lease() is not None
        host_states = host_deploy_state.read_all()
    except Exception:
        return dict.fromkeys(names, False)
    return {
        name: cluster_deploy_live or (name in host_states and host_states[name].updater_live)
        for name in names
    }


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


async def run_liveness_pass(
    pool: ConnectionPool, probe: Callable[..., Awaitable[object]] = cluster_rpc.dispatch_to_machine
) -> None:
    """One liveness pass: probe every agent-runner machine, then merge.

    `probe` is injectable for tests (default: the real cluster RPC). Probe
    failures are per-machine and quiet — a down host is steady-state; the
    pass keeps running for the hosts that are up.
    """
    runners = list_agent_runners()
    if not runners:
        return
    deploy_explanations = _deploy_explanations([name for name, _url in runners])
    results = await asyncio.gather(*(_probe_machine(name, probe=probe) for name, _url in runners))
    for (name, _url), ok in zip(runners, results, strict=True):
        await _record_probe(pool, name, ok=ok, deploy_explains=deploy_explanations[name])
    changed_agent_ids = _merge_liveness(pool)
    if changed_agent_ids:
        # `_merge_liveness` committed before this best-effort live projection.
        # Reuse one connection for the canonical snapshots so a host edge does
        # not open one Postgres connection per affected agent.
        with pool.connection() as conn:
            for agent_id in changed_agent_ids:
                publish_agent_updated_sync(conn, agent_id)
    _log.info(
        "[heartbeat] liveness pass: %d machines probed (%d reachable), agents_meta merged",
        len(runners),
        sum(results),
    )
