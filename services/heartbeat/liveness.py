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
  means a dead/wedged process. `hibernating` is lease-exempt by design (swapped
  out, no renewal). An unclaimed `idling` row has no process yet, so it stays
  `unknown` until its atomic claim writes `started_at`; `restarting` judges on
  machine reachability alone.

Per-agent merge (`liveness_state`):

- unclaimed idling -> 'unknown' (no process ownership yet)
- machine offline  -> 'offline' (whole host unreachable)
- running/idling with an expired (or never granted) lease -> 'offline'
- everything else on a reachable machine -> 'online'
- 'terminated' rows are never judged; rows whose machine is not in the
  machines table (or that the gateway has not judged yet) stay 'unknown',
  which the frontend renders conservatively as online.

`status` stays lifecycle intent — the pass never transitions it (R1 invariant
#1). A machine coming back is self-healing: its restarter revives the rows
(G5) and the next pass re-marks them online.

The probe path is injectable (`probe` argument) so tests can run the full
DB merge without dialing real ops servers.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from psycopg_pool import ConnectionPool

from ops import cluster_rpc
from shared.config import settings
from shared.live_announce import publish_agent_updated_sync
from shared.machines import list_agent_runners

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
_OFFLINE_AFTER_FAILURES = 2

# How often the pass runs. Independent of the check-in dispatch step (15s).
_PASS_INTERVAL_S = 60.0


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


_MACHINE_ALERT_SEVERITY = "error"  # one whole host down = incident class, not critical


def _machine_alert_labels(name: str) -> dict[str, str]:
    """The label set of a machine offline alert — identical on the firing and
    the recovery edge so both flips share one fingerprint (the dedup key)."""

    return {"alertname": "machine offline", "machine": name, "severity": _MACHINE_ALERT_SEVERITY}


def _machine_alert_edges(
    conn: Any, name: str, *, ok: bool, old_online: bool | None, new_cf: int
) -> None:
    """Machine offline/online edges -> alerts rows + IM (Task #1224).

    Direct write, ``source="machine-probe"`` — the liveness pass runs on the
    gateway with the DB at hand, so unlike the health probe there is no HTTP
    hop: ``shared.alerts`` is called straight (the same core the gateway
    ingest runs). The firing edge is the probe where the consecutive-failure
    count first reaches ``_OFFLINE_AFTER_FAILURES``; the recovery edge is the
    first successful probe after a judged-offline row. Steady-state failures
    (a machine that stays offline) stay silent — the pass runs once a minute
    and must not turn a persistent outage into a notification storm.

    Best-effort: alerting is a side channel and must never break the pass
    (DB errors propagate to the caller's per-pass catch, IM errors are
    swallowed by ``notify_im``).
    """
    from datetime import UTC, datetime

    from shared.alerts import (
        display_language,
        fingerprint,
        notify_im,
        notify_text,
        stamp_notified,
        upsert_alert,
    )

    labels = _machine_alert_labels(name)
    fp = fingerprint(labels)
    lang = display_language(conn)

    if not ok:
        # The firing edge is the probe where the consecutive-failure count
        # first reaches the offline threshold (cf resets on success, so the
        # count == threshold exactly once per outage episode). Steady-state
        # failures re-run the upsert against the OPEN instance instead, so a
        # firing IM that never landed (im_bridge down at the edge) retries
        # every pass while ``notified_at`` stays NULL — one persistent outage
        # must not go permanently unheard.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT starts_at, notified_at FROM alerts "
                "WHERE fingerprint = %s AND status = 'unresolved' "
                "ORDER BY starts_at DESC LIMIT 1",
                (fp,),
            )
            open_row = cur.fetchone()
        if open_row is None and new_cf >= _OFFLINE_AFTER_FAILURES:
            # Fires on the first pass at/over the threshold with no open
            # instance: a machine that went offline before this process
            # started (cf carried over in machine_probe, e.g. 234) must not
            # stay invisible until its NEXT outage — `>=` not `==`, or a
            # count that jumps past the threshold without ever landing on it
            # would never produce the firing edge.
            starts_at = datetime.now(UTC).isoformat()
        elif open_row is not None and open_row[1] is None:
            starts_at = open_row[0].isoformat()
        else:
            return  # already notified, or not yet past the threshold
        alert = {
            "status": "firing",
            "labels": labels,
            "annotations": {
                "summary": f"machine {name} offline: {new_cf} consecutive failed probes"
            },
            "starts_at": starts_at,
            "fingerprint": fp,
        }
        key, _did_insert, should_notify, _row = upsert_alert(conn, alert, source="machine-probe")
        if should_notify and notify_im(notify_text(alert, lang)):
            stamp_notified(conn, [key])
        return

    if ok and old_online is False:
        # Recovery: flip every still-unresolved instance of this machine's
        # alert. The firing edge's starts_at is replayed so both flips
        # resolve as one row.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT starts_at FROM alerts WHERE fingerprint = %s AND status = 'unresolved' "
                "ORDER BY starts_at DESC",
                (fp,),
            )
            open_rows = [r[0] for r in cur.fetchall()]
        for starts_at in open_rows:
            alert = {
                "status": "resolved",
                "labels": labels,
                "annotations": {"summary": f"machine {name} back online"},
                "starts_at": starts_at.isoformat(),
                "ends_at": datetime.now(UTC).isoformat(),
                "fingerprint": fp,
            }
            key, _did_insert, should_notify, _row = upsert_alert(
                conn, alert, source="machine-probe"
            )
            if should_notify and notify_im(notify_text(alert, lang)):
                stamp_notified(conn, [key])


async def _record_probe(pool: ConnectionPool, name: str, *, ok: bool) -> None:
    """UPSERT one probe outcome into machine_probe, bumping the consecutive
    failure count on failure and resetting it on success — and record the
    offline/online edge as an alerts row (see ``_machine_alert_edges``)."""
    with pool.connection() as conn:
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
                "INSERT INTO machine_probe (machine_name, online, consecutive_failures, last_probe_at) "
                "VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (machine_name) DO UPDATE SET "
                "  online = EXCLUDED.online, "
                "  consecutive_failures = EXCLUDED.consecutive_failures, "
                "  last_probe_at = now()",
                (name, ok, new_cf),
            )
        _machine_alert_edges(conn, name, ok=ok, old_online=old_online, new_cf=new_cf)


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
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "WITH probe AS ("
            "  SELECT m.name AS machine_name,"
            "         COALESCE(mp.consecutive_failures, 0) AS cf"
            "  FROM machines m"
            "  LEFT JOIN machine_probe mp ON mp.machine_name = m.name"
            "), desired AS ("
            "  SELECT a.id, a.liveness_state AS old_liveness_state,"
            "    CASE "
            "      WHEN a.status = 'idling' AND a.started_at IS NULL THEN 'unknown' "
            "      WHEN p.cf >= %s THEN 'offline' "
            "      WHEN a.status IN ('running', 'idling') AND a.started_at IS NOT NULL "
            "           AND (a.lease_expires_at IS NULL OR a.lease_expires_at <= now()) "
            "        THEN 'offline' "
            "      ELSE 'online' "
            "    END AS new_liveness_state "
            "  FROM agents_meta a "
            "  JOIN probe p ON a.machine = p.machine_name "
            "  WHERE a.status != 'terminated'"
            ") "
            "UPDATE agents_meta a "
            "SET liveness_state = d.new_liveness_state, "
            "    last_probe_at = now() "
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
    results = await asyncio.gather(*(_probe_machine(name, probe=probe) for name, _url in runners))
    for (name, _url), ok in zip(runners, results, strict=True):
        await _record_probe(pool, name, ok=ok)
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
