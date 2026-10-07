"""Native alert source facts and revision-fenced completion, without transport ownership."""

import logging
from enum import StrEnum
from typing import Any

import httpx
from psycopg import Connection
from psycopg.rows import dict_row

from base.cluster.machine import GatewayApiTokenMissing, gateway_auth_headers
from base.config import settings
from base.telemetry.alerts import im_bridge_rpc_url

_log = logging.getLogger(__name__)


class AlertGroupOrigin(StrEnum):
    SHADOW = "shadow"
    NATIVE = "native-v1"


class NativeAlertSourceError(ValueError):
    """A requested group does not carry the native immutable source contract."""


def load_native_group(conn: Connection, group_id: int, *, lock: bool = False) -> dict[str, Any]:
    if type(group_id) is not int or not 0 < group_id < 2**63:
        raise NativeAlertSourceError("a positive alert group ID is required")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id,status,alertname,language,render_version,text,origin "
            "FROM alert_notification_groups WHERE id=%s" + (" FOR UPDATE" if lock else ""),
            (group_id,),
        )
        group = cur.fetchone()
        if group is None or group["origin"] != AlertGroupOrigin.NATIVE:
            raise NativeAlertSourceError("alert group has no native source origin")
        cur.execute(
            "SELECT alert_id,notification_revision,ordinal,reason FROM alert_notification_members "
            "WHERE group_id=%s ORDER BY ordinal",
            (group_id,),
        )
        members = cur.fetchall()
    if not members or any(member["reason"] == "legacy_unconfirmed" for member in members):
        raise NativeAlertSourceError("native alert group has an unqualified membership")
    if group["render_version"] != "alert-group-v1":
        raise NativeAlertSourceError("unsupported native alert render version")
    return {**group, "members": members}


def stamp_native_sent(conn: Connection, group_id: int) -> int:
    """Any real channel SENT completes only the current source revision/status.

    notified_at retains its existing first-ever timestamp semantics. A legacy
    timestamp cannot populate notified_revision, which is native SENT evidence.
    """
    return conn.execute(
        "UPDATE alerts a SET notified_revision=m.notification_revision, "
        "notified_at=COALESCE(a.notified_at,now()) "
        "FROM alert_notification_members m JOIN alert_notification_groups g ON g.id=m.group_id "
        "WHERE g.id=%s AND g.origin='native-v1' AND a.id=m.alert_id "
        "AND a.notification_revision=m.notification_revision AND a.status=g.status "
        "AND a.notified_revision<m.notification_revision",
        (group_id,),
    ).rowcount


def native_sent_count(conn: Connection, group_ids: set[int]) -> int:
    row = conn.execute(
        "SELECT count(*) FROM alert_notification_members m JOIN alerts a ON a.id=m.alert_id "
        "JOIN alert_notification_groups g ON g.id=m.group_id "
        "WHERE g.origin='native-v1' AND a.status=g.status AND m.group_id=ANY(%s) AND a.notification_revision=m.notification_revision "
        "AND a.notified_revision=m.notification_revision",
        (list(group_ids),),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def notify_alert_group(group_id: int) -> bool:
    """Request durable acceptance; never fall back to legacy /send or stamp SENT."""
    if not settings.alerts.im_notify_enabled:
        return False
    base = im_bridge_rpc_url()
    try:
        response = httpx.post(
            f"{base}/send/alert-outbound-v1",
            json={"group_id": group_id, "source_origin": AlertGroupOrigin.NATIVE.value},
            headers=gateway_auth_headers(),
            timeout=10.0,
        )
        if response.status_code == 200:
            return True
        _log.warning(
            "native alert acceptance held group=%s status=%s", group_id, response.status_code
        )
    except (httpx.HTTPError, GatewayApiTokenMissing) as exc:
        _log.warning("native alert acceptance held group=%s class=%s", group_id, type(exc).__name__)
    return False
