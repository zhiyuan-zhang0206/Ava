"""Alert ingest core — the alerts-store + IM-notification logic, shared.

One implementation serves every writer of the ``alerts`` table (Task #1224,
user design 2026-08-12: Alert fully separate from Notice — own table, own UI,
own IM channel):

- the gateway router (gateway/routers/alerts.py) ingests the Grafana
  embedded-Alertmanager webhook on ``POST /api/alerts``;
- the cluster health probe (cli/commands/_health_alerts.py) posts its
  edge-triggered health alerts through that endpoint too
  (``source="health-probe"``) — and when the gateway is unreachable, it runs
  these same functions locally against its own DB connection, so one health
  alert = one alerts row + one IM notification even mid-outage;
- the heartbeat liveness pass (services/heartbeat/liveness.py) writes its
  machine offline/online edges straight to the table (``source="machine-probe"``).

The functions take the Alertmanager webhook *alert shape as a plain dict*
(status/labels/annotations/starts_at/ends_at/fingerprint/generator_url) so
this module stays below both the gateway layer and the CLI layer; callers own
their transport (router: pydantic + the app's DB pool; probes: their own
payload dict + ``shared.db.connect``).

Dedup key (fingerprint, starts_at): Alertmanager re-sends the same instance
every group_interval while it is firing and once more on resolution — the
upsert updates the row instead of duplicating it; a resolved row that fires
again (new starts_at) starts a fresh instance. ``fingerprint`` is the
Alertmanager-standard fnv-1a hash over sorted labels; the ingest computes it
when a direct writer omits it, using the exact Alertmanager algorithm so a
computed hash and a Grafana-sent hash agree for the same label set. The IM
fan-out goes through the local im_bridge daemon's health-port ``/send`` RPC,
gated by the transition logic in ``upsert_alert`` — every severity pushes
unless the rule explicitly labels the alert ``notify_im="false"``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from services.im_bridge import copy as im_copy
from shared.config import settings
from shared.daemon_health import health_port

_log = logging.getLogger(__name__)

# (fingerprint, starts_at) — the dedup key for one alert instance.
AlertKey = tuple[str, datetime]

# Alertmanager sends the zero time ("0001-01-01T00:00:00Z") for the not-yet-known
# endsAt of a firing alert — treat it as NULL, not a real timestamp.
_ZERO_TS = frozenset({"0001-01-01T00:00:00Z", "0001-01-01T00:00:00+00:00", ""})

# The three severity classes (user design 2026-08-12). Anything else a rule
# label carries normalizes to ``warning`` — the quietest class, so an
# unlabelled rule never reads as an incident.
_SEVERITIES = frozenset({"critical", "warning", "error"})

# Alertmanager's status vocabulary maps onto the store's: the store has only
# unresolved / resolved (no ack, no escalation — user ruling).
_STATUS_MAP = {"firing": "unresolved", "resolved": "resolved"}

_IM_GATE_LABEL = "notify_im"
_IM_GATE_VALUE = "false"

# FNV-1a 64-bit constants (the hash Alertmanager fingerprints labels with).
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF


def parse_ts(raw: str) -> datetime | None:
    """RFC3339 -> aware datetime; None for empty / Alertmanager's zero time."""

    if not raw or raw in _ZERO_TS:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


# Legacy P0-P3 vocabulary (the pre-Task-#1224 ops-alerts rules.yml labels)
# maps onto the new classes so a rule not yet updated lands at the right
# tier instead of silently downgrading to ``warning``. Same mapping the
# data migration uses.
_LEGACY_SEVERITY_MAP = {"P0": "critical", "P1": "error", "P2": "warning", "P3": "warning"}


def parse_severity(labels: dict[str, str]) -> str:
    """Normalize a rule's severity label to critical/warning/error.

    Accepts the new vocabulary plus the legacy P0-P3 labels (compat shim —
    see ``_LEGACY_SEVERITY_MAP``). ``warning`` when absent/unparseable —
    the quietest default, so an unlabelled rule never spams the user."""

    raw = str(labels.get("severity") or "").strip()
    lower = raw.lower()
    if lower in _SEVERITIES:
        return lower
    return _LEGACY_SEVERITY_MAP.get(raw.upper(), "warning")


def parse_alertname(labels: dict[str, str]) -> str:
    """Human rule title: Alertmanager sets ``alertname`` to the rule name."""

    return labels.get("alertname") or "unknown"


def fingerprint(labels: dict[str, str]) -> str:
    """The Alertmanager-standard fingerprint of a label set: fnv-1a 64-bit
    over each sorted ``name`` + 0xff separator + ``value`` + 0xff separator,
    rendered as the decimal string (Alertmanager's ``Fingerprint().String()``).

    Implemented exactly so a fingerprint the gateway computes for a direct
    writer equals the one Grafana's embedded Alertmanager sends for the same
    labels — the (fingerprint, starts_at) dedup key then stays stable across
    the two paths."""

    h = _FNV_OFFSET
    for name in sorted(labels):
        for chunk in (name.encode(), b"\xff", str(labels[name]).encode(), b"\xff"):
            for byte in chunk:
                h ^= byte
                h = (h * _FNV_PRIME) & _FNV_MASK
    return str(h)


def normalize_status(status: str) -> str:
    """Webhook status (firing/resolved) -> store status (unresolved/resolved)."""

    return _STATUS_MAP.get(status, "unresolved")


def im_fanout_allowed(labels: dict[str, str]) -> bool:
    """Return whether this alert may fan out to IM.

    Only the exact string ``labels["notify_im"] == "false"`` disables IM.
    This conservative gate keeps noisy slow-request rules visible in the UI
    and Insights while keeping the user's IM quiet (Task #1404).
    """

    return labels.get(_IM_GATE_LABEL) != _IM_GATE_VALUE


def upsert_alert(
    conn: psycopg.Connection, alert: dict[str, Any], source: str = "grafana"
) -> tuple[AlertKey, bool, bool, dict[str, Any]]:
    """Upsert one alert instance; return (key, did_insert, should_notify, row).

    ``alert`` is the Alertmanager-webhook alert shape as a dict (the model
    dump / a probe's payload dict). ``source`` tags the row's provenance
    ('grafana' | 'health-probe' | 'machine-probe'); it is set on insert and
    never overwritten by a conflict re-send — a repeated webhook or probe
    edge for the same instance must not rewrite history.

    ``should_notify`` — the notification gate (every severity pushes unless
    ``notify_im`` is exactly ``"false"``; transition rules still prevent a
    repeated re-send of a still-firing instance from re-spamming):
    - firing, and the firing has not been IM-notified yet (fresh insert, a
      failed earlier attempt, or a resolved row re-firing): True — a single
      im_bridge outage must not silence the alert forever, the next re-send
      retries while ``notified_at`` stays NULL
    - firing, re-fired after a resolution (a new event for the user): True
    - instance resolved, and the firing had been IM-notified before: True
    - every other re-send (already firing + already notified, already
      resolved): False
    """

    labels: dict[str, str] = alert.get("labels") or {}
    status = normalize_status(str(alert.get("status") or "firing"))
    severity = parse_severity(labels)
    alertname = parse_alertname(labels)
    fp = alert.get("fingerprint") or fingerprint(labels)
    starts_at = parse_ts(alert.get("starts_at") or "")

    if starts_at is None:
        # No starts_at (payload drift): re-sends of an EXISTING instance
        # reuse its starts_at so the (fingerprint, starts_at) dedup key stays
        # stable. A genuinely new alert with no starts_at has no stable
        # identity — fabricating now() would make every re-send a fresh
        # instance (duplicate rows + duplicate IMs), so it is rejected.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT starts_at FROM alerts WHERE fingerprint = %s "
                "ORDER BY starts_at DESC LIMIT 1",
                (fp,),
            )
            row = cur.fetchone()
        if row is not None:
            starts_at = row[0]
        else:
            _log.warning(
                "alerts: alert %r has no starts_at and no existing instance "
                "— rejected (dedup key would be unstable)",
                alertname,
            )
            return (fp, datetime.min.replace(tzinfo=UTC)), False, False, {}

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT status, notified_at FROM alerts WHERE fingerprint = %s AND starts_at = %s",
            (fp, starts_at),
        )
        old = cur.fetchone()
        old_status = old["status"] if old else None
        was_notified = old is not None and old["notified_at"] is not None

        cur.execute(
            "INSERT INTO alerts"
            " (status, severity, alertname, labels, annotations, starts_at, ends_at,"
            "  fingerprint, generator_url, source)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (fingerprint, starts_at) DO UPDATE SET"
            "  status = EXCLUDED.status,"
            "  severity = EXCLUDED.severity,"
            "  alertname = EXCLUDED.alertname,"
            "  labels = EXCLUDED.labels,"
            "  annotations = EXCLUDED.annotations,"
            "  ends_at = EXCLUDED.ends_at,"
            "  generator_url = EXCLUDED.generator_url,"
            "  updated_at = now()"
            " RETURNING id, status, severity, alertname, labels, annotations, starts_at,"
            "           ends_at, fingerprint, generator_url, source, read_at, notified_at,"
            "           created_at, updated_at, (xmax = 0) AS inserted",
            (
                status,
                severity,
                alertname,
                Jsonb(labels),
                Jsonb(alert.get("annotations") or {}),
                starts_at,
                parse_ts(alert.get("ends_at") or ""),
                fp,
                str(alert.get("generator_url") or ""),
                source,
            ),
        )
        # xmax = 0 distinguishes the fresh INSERT from the ON CONFLICT UPDATE
        # (rowcount is 1 on both paths).
        row = cur.fetchone()
        assert row is not None  # noqa: S101 — upsert always returns a row
        did_insert = bool(row.pop("inserted"))

    should_notify = False
    if status == "unresolved" and (old_status == "resolved" or not was_notified):
        # Firing gate is ``notified_at IS NULL``: keep notifying on every
        # re-send until the message actually lands (was_notified), and on a
        # re-fire after a resolution regardless (new event for the user).
        should_notify = True
    elif old_status == "unresolved" and status == "resolved" and was_notified:
        should_notify = True
    should_notify = im_fanout_allowed(labels) and should_notify
    return (fp, starts_at), did_insert, should_notify, row


def stamp_notified(conn: psycopg.Connection, keys: list[AlertKey]) -> None:
    """Record notified_at on the instances a successful IM send covered."""

    if not keys:
        return
    with conn.cursor() as cur:
        for fp, starts_at in keys:
            cur.execute(
                "UPDATE alerts SET notified_at = now()"
                " WHERE fingerprint = %s AND starts_at = %s AND notified_at IS NULL",
                (fp, starts_at),
            )


def notify_im(text: str) -> bool:
    """POST one message to the local im_bridge daemon's ``/send`` RPC.

    The daemon fans the message out to every loaded IM adapter's owner chat
    (Telegram / WeChat / Feishu) — the only sanctioned IM surface. Failures
    are logged and swallowed — the ingest must not fail because the IM side
    is down; notified_at simply stays NULL and the firing gate retries on
    the next re-send.

    Delivery semantics: at-most-once, never silent — the failure is visible
    (log + False).
    """

    if not settings.alerts.im_notify_enabled:
        return False
    base = (settings.services.im_bridge_health_url or "").rstrip("/") or (
        f"http://127.0.0.1:{health_port('im_bridge')}"
    )
    try:
        resp = httpx.post(
            f"{base}/send",
            json={"text": text, "type": "alert"},
            headers={"Authorization": f"Bearer {settings.data_plane.cluster_secret}"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            return True
        _log.warning("alerts: im_bridge /send returned HTTP %s", resp.status_code)
    except httpx.HTTPError:
        _log.warning("alerts: im_bridge /send failed", exc_info=True)
    return False


def format_local(ts: datetime | None) -> str:
    """Machine-local wall-clock string for the IM message ('' when None).

    Carries year + zone abbreviation (tz audit, 2026-08, PR-6): the prior
    `%m-%d %H:%M` gave no way to tell which year an alert near New Year's
    fired in, and no way to tell which machine's local zone the reader was
    looking at on a multi-machine cluster. Minimal fix — stays on the
    gateway machine's local zone (`.astimezone()` with no explicit tz) rather
    than switching to the cluster's `AVA_TIMEZONE`, since that would need a
    settings read this module doesn't otherwise take a dependency on.
    """

    if ts is None:
        return ""
    return ts.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def frontend_base_url() -> str:
    """The user-facing fleet UI base URL for IM jump links.

    The user reaches the fleet UI at the always-up gate's entry port on the
    gateway host (the Next.js app itself binds another port and is proxied).
    Derived from the two existing settings: the HOST of ``AVA_GATEWAY_URL``
    (reachable over the private network, never localhost in prod) and the PORT of
    ``AVA_FRONTEND_HEALTHCHECK_URL`` (the fleet UI entry the user reaches).
    """

    gw = urlsplit(settings.gateway.gateway_url or "")
    fe = urlsplit(settings.services.frontend_healthcheck_url or "")
    host = gw.hostname or fe.hostname
    if not host:
        # No gateway URL and no healthcheck URL configured — no reachable
        # fleet UI to link; never fall back to a loopback address.
        return ""
    port = fe.port or 3000
    return f"{gw.scheme or 'http'}://{host}:{port}"


def _summary(alert: dict[str, Any]) -> str:
    raw = alert.get("annotations")
    annotations: dict[str, str] = cast("dict[str, str]", raw) if isinstance(raw, dict) else {}
    return str(annotations.get("summary") or annotations.get("description") or "")


def display_language(conn: psycopg.Connection) -> str:
    """The IM template language — ``user_settings`` key ``display.language``.

    IM copy language follows the UI language (user ruling 2026-08-13: one
    language source, no separate IM field). Returns "zh" | "en"; a missing
    row or an unknown value falls back to ``im_copy.ALERT_LANGUAGE_DEFAULT``
    ("zh"). Only template/framework copy is translated — alert data never is.
    """

    with conn.cursor() as cur:
        cur.execute("SELECT value FROM user_settings WHERE key = 'display.language'")
        row = cur.fetchone()
    raw = row[0] if row else None
    return raw if raw in im_copy.ALERT_LANGUAGES else im_copy.ALERT_LANGUAGE_DEFAULT


def notify_text(alert: dict[str, Any], lang: str | None = None) -> str:
    """The alert IM message — the alert-specific format (user design 2026-08-12):
    a warning-sign-prefixed ``ALERT [<severity>] <alertname>`` head line, the
    summary body, the generatorURL when present, the trigger time, and the
    fleet-UI jump link (omitted when no fleet-UI base URL is configured).
    Resolved instances swap the head for the check-marked ``RESOLVED``
    variant. Every severity pushes
    (critical/warning/error — no severity gate).

    Templates live in ``services/im_bridge/copy.py`` — the single source of
    user-visible IM copy (governance ruling 2026-08-08). ``lang`` picks the
    template language ("zh" | "en"); ``None`` (or an unknown value) falls back
    to ``im_copy.ALERT_LANGUAGE_DEFAULT`` ("zh", user ruling 2026-08-13).
    Alert labels/annotations data (severity, alertname, summary,
    generator_url) passes through untranslated.
    """

    labels: dict[str, str] = alert.get("labels") or {}
    severity = parse_severity(labels).upper()
    alertname = parse_alertname(labels)
    resolved = normalize_status(str(alert.get("status") or "")) == "resolved"
    lang = lang if lang in im_copy.ALERT_LANGUAGES else im_copy.ALERT_LANGUAGE_DEFAULT
    head = im_copy.ALERT_HEAD[lang]["resolved" if resolved else "firing"].format(
        severity=severity, alertname=alertname
    )
    lines = [head]
    summary = _summary(alert)
    if summary:
        lines.append(summary[:200])
    if alert.get("generator_url"):
        lines.append(alert["generator_url"])
    start = format_local(parse_ts(alert.get("starts_at") or ""))
    if start:
        lines.append(im_copy.ALERT_TRIGGERED_AT[lang].format(time=start))
    base_url = frontend_base_url()
    if base_url:
        lines.append(im_copy.ALERT_JUMP_LINK.format(url=base_url))
    return "\n".join(lines)
