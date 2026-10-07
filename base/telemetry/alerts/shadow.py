"""Immutable alert transition/group facts; native origin never promotes shadow history."""

import json
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from base.telemetry.alerts import (
    AlertKey,
    alert_instance_fingerprint,
    normalize_status,
    notify_group_text,
    parse_alertname,
    resolve_alert_key,
)
from base.telemetry.alerts.native import AlertGroupOrigin


class ShadowTransitionReason(StrEnum):
    FRESH_FIRING = "fresh_firing"
    REFIRE = "refire"
    ESCALATION = "escalation"
    RESOLUTION = "resolution"
    LEGACY_UNCONFIRMED = "legacy_unconfirmed"


@dataclass(frozen=True)
class ShadowMember:
    alert_id: int
    revision: int
    reason: ShadowTransitionReason
    key: AlertKey
    alert: dict[str, Any]


class AlertShadowBatch:
    """Own one ingest transaction's facts; never send or accept provider delivery.

    Fingerprint gates are acquired in sorted order before resolving start times
    and row locks. This covers absent instances and latest-start lookup drift.
    Input order is retained for resolution, decisions and rendering. The caller
    commits facts with the corresponding alert updates.
    """

    def __init__(
        self,
        conn: psycopg.Connection,
        alerts: list[dict[str, Any]],
        language: str,
        *,
        native: bool = False,
    ) -> None:
        self.conn = conn
        self.language = language
        self.native = native
        self.native_ids: set[int] = set()
        self.members: dict[tuple[str, str, AlertGroupOrigin], list[ShadowMember]] = defaultdict(
            list
        )
        self.items = alerts
        for fp in sorted({alert_instance_fingerprint(alert) for alert in alerts}):
            identity = json.dumps(["alert-shadow-fingerprint", fp])
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,4477))", (identity,))

    def observe(self, alert: dict[str, Any]) -> tuple[AlertKey | None, dict[str, Any] | None]:
        """Resolve in input order, then lock the instance before the upsert mutates it."""
        key = resolve_alert_key(self.conn, alert)
        if key is None:
            return None, None
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT status,severity,notified_at,notification_revision FROM alerts "
                "WHERE fingerprint=%s AND starts_at=%s FOR UPDATE",
                key,
            )
            return key, cur.fetchone()

    def record(
        self,
        alert: dict[str, Any],
        row: dict[str, Any],
        previous: dict[str, Any] | None,
        *,
        should_notify: bool,
    ) -> None:
        """Mint only a new policy transition; repeated unconfirmed observations reuse facts."""
        if not should_notify:
            return
        reason = self._reason(row, previous)
        if reason is None:
            previous_group = self.conn.execute(
                "SELECT g.id FROM alert_notification_members m "
                "JOIN alert_notification_groups g ON g.id=m.group_id "
                "WHERE m.alert_id=%s AND m.notification_revision=%s AND g.origin='native-v1'",
                (row["id"], previous["notification_revision"] if previous else 0),
            ).fetchone()
            if previous_group is not None:
                self.native_ids.add(int(previous_group[0]))
            return
        revision = self.conn.execute(
            "UPDATE alerts SET notification_revision=notification_revision+1 WHERE id=%s "
            "RETURNING notification_revision",
            (row["id"],),
        ).fetchone()
        assert revision is not None  # noqa: S101 — the upsert just returned this row
        member = ShadowMember(
            alert_id=row["id"],
            revision=revision[0],
            reason=reason,
            key=(row["fingerprint"], row["starts_at"]),
            alert=alert,
        )
        group = (
            normalize_status(str(alert.get("status") or "")),
            parse_alertname(alert.get("labels") or {}),
            AlertGroupOrigin.NATIVE
            if self.native and reason != ShadowTransitionReason.LEGACY_UNCONFIRMED
            else AlertGroupOrigin.SHADOW,
        )
        self.members[group].append(member)

    @staticmethod
    def _reason(
        row: dict[str, Any], previous: dict[str, Any] | None
    ) -> ShadowTransitionReason | None:
        if previous is None:
            return ShadowTransitionReason.FRESH_FIRING
        if row["status"] == "resolved":
            return ShadowTransitionReason.RESOLUTION
        if previous["status"] == "resolved":
            return ShadowTransitionReason.REFIRE
        if previous["notified_at"] is not None:
            # The established should_notify policy already proved an escalation.
            return ShadowTransitionReason.ESCALATION
        if previous["notification_revision"] == 0:
            return ShadowTransitionReason.LEGACY_UNCONFIRMED
        return None

    def freeze(self) -> None:
        """Freeze newly minted groups using the existing renderer and member order."""
        for (status, alertname, origin), members in self.members.items():
            text = notify_group_text([member.alert for member in members], self.language)
            group = self.conn.execute(
                "INSERT INTO alert_notification_groups(status,alertname,language,render_version,text,origin) "
                "VALUES (%s,%s,%s,'alert-group-v1',%s,%s) RETURNING id",
                (status, alertname, self.language, text, origin.value),
            ).fetchone()
            assert group is not None  # noqa: S101 — INSERT RETURNING always yields one row
            if origin == AlertGroupOrigin.NATIVE:
                self.native_ids.add(int(group[0]))
            for ordinal, member in enumerate(members):
                self.conn.execute(
                    "INSERT INTO alert_notification_members "
                    "(alert_id,notification_revision,group_id,ordinal,reason,fingerprint,starts_at,source) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        member.alert_id,
                        member.revision,
                        group[0],
                        ordinal,
                        member.reason.value,
                        *member.key,
                        Jsonb(member.alert),
                    ),
                )
