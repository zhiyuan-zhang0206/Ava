"""Deliver one journaled fleet alert along one route (`alerting.deliveries`).

- **alert row**: `shared.telemetry.alerts.upsert_alert` with source `release-fleet`; the
  table deduplicates by `(fingerprint, starts_at)`, so a retried delivery that
  already landed changes nothing.
- **webhook**: an HTTP POST of the JSON body to the URL held in the
  coordinator's `$AVA_HOME/secrets/<file>` (owner-only). It needs neither the
  database nor the gateway, so it reaches a person when the cluster is down.
  A retry may repeat it; the body's `key` identifies the alert.
- **agent notice**: one system inbound message to the observing agent, claimed
  exactly once through `api_idempotency` (the closure-notice shape).
"""

from __future__ import annotations

import json
import os
import stat
import urllib.request
from pathlib import Path

from cli.release_fleet.alerting import AgentNotice, AlertRow, Delivery, FleetAlert, Webhook

_WEBHOOK_TIMEOUT_S = 10.0
_MAX_URL_BYTES = 4096


def deliver_one(home: Path, alert: FleetAlert, delivery: Delivery) -> None:
    """Deliver or raise; the caller journals only a delivery that returned."""
    if isinstance(delivery, AlertRow):
        _alert_row(delivery)
    elif isinstance(delivery, Webhook):
        _webhook(home, delivery)
    elif isinstance(delivery, AgentNotice):
        _agent_notice(alert, delivery)
    else:
        raise TypeError(f"unknown fleet alert delivery {type(delivery).__name__}")


def _alert_row(row: AlertRow) -> None:
    from shared.db_transaction import write_transaction
    from shared.telemetry.alerts import upsert_alert

    with write_transaction() as conn:
        upsert_alert(conn, row.payload(), source=row.source)


def webhook_url(home: Path, url_file: str) -> str:
    """The owner-only secrets file's single https or http URL.

    The URL is usually a bearer in itself, so the file must be this user's
    and no one else's to read or write (0600); anything wider is refused.
    """
    from shared.deploy.release.verified_file import regular_bytes
    from shared.host.private_storage import private_file_problem

    path = home / "secrets" / url_file
    if problem := private_file_problem(path):
        raise ValueError(f"fleet alert webhook file {problem}")
    info = path.lstat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError(
            f"fleet alert webhook file {path} must be owner-only: owned by this user, mode 0600"
        )
    url = regular_bytes(path, max_bytes=_MAX_URL_BYTES).decode().strip()
    if not url.startswith(("https://", "http://")) or any(c.isspace() for c in url):
        raise ValueError("the fleet alert webhook file holds no single http(s) URL")
    return url


def _webhook(home: Path, webhook: Webhook) -> None:
    body = webhook.body.model_dump_json().encode()
    request = urllib.request.Request(  # noqa: S310 — operator-configured http(s) URL, checked above
        webhook_url(home, webhook.url_file),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_WEBHOOK_TIMEOUT_S) as response:  # noqa: S310 — same URL
        if not 200 <= response.status < 300:
            raise RuntimeError(f"the fleet alert webhook answered {response.status}")


def _agent_notice(alert: FleetAlert, notice: AgentNotice) -> None:
    """Exactly once per alert key; a terminated observer is never resurrected."""
    from shared.agents.messages.inbound_provenance import InboundProvenance
    from shared.db import insert_inbound_message, publish_inbound_wake
    from shared.db_transaction import write_transaction

    with write_transaction() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_idempotency (key, method, path, response_body, op_status, "
            "completed_at) VALUES (%s, 'ops', 'release-fleet-alert', %s, 'completed', now()) "
            "ON CONFLICT (key) DO NOTHING RETURNING key",
            (f"release-fleet:{alert.key}", json.dumps({"agent": notice.agent})),
        )
        if cur.fetchone() is None:
            return
        cur.execute("SELECT status FROM agents_meta WHERE id = %s", (notice.agent,))
        row = cur.fetchone()
        if row is None or row[0] not in {"running", "idling"}:
            return
        inbound = insert_inbound_message(
            conn,
            notice.agent,
            notice.text,
            source="system",
            payload={"release_fleet": {"key": alert.key, "event": alert.kind}},
            provenance=InboundProvenance(source_verified_by=None, source_transport="ops"),
        )
    publish_inbound_wake(notice.agent, str(inbound))
