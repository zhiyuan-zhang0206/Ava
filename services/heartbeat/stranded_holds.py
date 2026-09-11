"""Stranded-hold alerting — grade `host_deploy_state.stranded_hold_*` into alert edges.

Two writers, one record (task #3132). The pause controller on each host declares
`stranded_hold_since/reason` while the host sits in a maintenance hold whose owner
is gone — the state a failed updater leg leaves — and clears it when the hold does
(see `ops.controllers.stranded_pause`). This pass, running in the gateway's
heartbeat daemon on the same slow cadence as the liveness pass, turns that record
into the user-facing alarm: a firing alert edge on the alerts store
(`source="deploy-probe"`, IM-fanned by the pipeline every other alert uses) while
a record exists, and a resolve edge when it clears.

It is deliberately the relay that survives the failure it reports: the held
host's own ops server is usually down with it, so nothing on the host can carry
the message past the point it went quiet — while the DB row the host wrote before
going down is readable from the gateway, and the gateway's own im_bridge is the
one that can still reach the user. Same shape as the machine-offline edges in
`services.heartbeat.liveness`: a stable identity fingerprint (alertname x
machine), notify/severity transitions gated by `upsert_alert`, direct table write
plus local `notify_im`, and best-effort failure handling — an alerting side
channel must never break the heartbeat loop.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

_log = logging.getLogger("services.heartbeat.stranded_holds")

# Stable identity of one host's alert instance: these labels (not the summary,
# not the severity) are the dedup key, so one held host = one running instance,
# re-notified only by the gates inside `upsert_alert`.
ALERTNAME = "update failed: host left held"
SOURCE = "deploy-probe"


def _summary(machine: str, reason: str | None) -> str:
    """The operator-facing sentence — what happened, and the one command that fixes it."""
    why = f" ({reason})" if reason else ""
    return (
        f"{machine}: update failed{why} and the host is left held — nothing will "
        f"resume it on its own; run `ava start` on {machine}"
    )


def grade_stranded_holds(pool: Any) -> None:
    """One pass: firing edges for held hosts, resolve edges for recovered ones.

    For every stranded-hold record, upsert a firing alert and attempt the IM push
    (the firing gate in `upsert_alert` retries while `notified_at` stays NULL, so
    a failed push is retried next pass, never silently dropped). Then resolve
    every open instance whose host no longer carries the record — including a host
    that recovered while this process was restarting: resolution is derived from
    the record, not from having witnessed the firing edge.
    """
    from shared.alerts import (
        display_language,
        fingerprint,
        notify_im,
        notify_text,
        stamp_notified,
        upsert_alert,
    )
    from shared.db_transaction import write_transaction

    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT machine, stranded_hold_since, stranded_hold_reason "
            "FROM host_deploy_state WHERE stranded_hold_since IS NOT NULL"
        )
        held: dict[str, tuple[datetime, str | None]] = {
            row[0]: (row[1], row[2]) for row in cur.fetchall()
        }
        cur.execute(
            "SELECT labels->>'machine', starts_at, labels->>'severity' FROM alerts "
            "WHERE labels->>'alertname' = %s AND status = 'unresolved'",
            (ALERTNAME,),
        )
        open_rows: list[tuple[str, datetime, str | None]] = [
            (row[0], row[1], row[2]) for row in cur.fetchall()
        ]
        lang = display_language(conn)
        for machine, (since, reason) in sorted(held.items()):
            identity = {"alertname": ALERTNAME, "machine": machine}
            alert = {
                "status": "firing",
                "labels": {**identity, "severity": "error"},
                "annotations": {"summary": _summary(machine, reason)},
                "starts_at": since.isoformat(),
                "fingerprint": fingerprint(identity),
            }
            key, _did_insert, should_notify, _row = upsert_alert(conn, alert, source=SOURCE)
            if should_notify and notify_im(notify_text(alert, lang)):
                stamp_notified(conn, [key])
        for machine, starts_at, severity in open_rows:
            if machine in held:
                continue
            alert = {
                "status": "resolved",
                "labels": {
                    "alertname": ALERTNAME,
                    "machine": machine,
                    "severity": severity or "error",
                },
                "annotations": {"summary": f"{machine}: the held update is released"},
                "starts_at": starts_at.isoformat(),
                "ends_at": datetime.now(UTC).isoformat(),
                "fingerprint": fingerprint({"alertname": ALERTNAME, "machine": machine}),
            }
            key, _did_insert, should_notify, _row = upsert_alert(conn, alert, source=SOURCE)
            if should_notify and notify_im(notify_text(alert, lang)):
                stamp_notified(conn, [key])
    if held or open_rows:
        _log.info(
            "[heartbeat] stranded-hold pass: %d held host(s), %d open alert instance(s)",
            len(held),
            len(open_rows),
        )
