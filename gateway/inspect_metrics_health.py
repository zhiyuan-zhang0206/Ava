"""Inspector metrics coverage verdicts — background log + alert routing (task #3869).

The inspector panel stopped rendering the read model's coverage verdicts
(user ruling 2026-09-17): "historical coverage is incomplete", a duration
precision note, a retained-source note — machinery, not user copy. The
backend keeps the record instead of the panel:

- every coverage limit yields ONE structured warning log line per
  (agent, family, condition) per the
  ``alerts.inspect_metrics_degraded_cooldown_seconds`` cooldown (default 1h).
  The cooldown is what keeps a read-path signal from re-logging the same
  chronic limit on every statistics request.
- limits expected by construction stop at that log line: a window reaching
  back before the observation collection started
  (``historical_coverage_unknown``), a legacy archive whose per-day precision
  cannot be attributed per turn (``archive_precision_unattributed``, incl.
  retained sources). Alerting them would only move the panel's chronic noise
  into the alerts surface — the same ruling grades expected gaps down.
- an UNEXPECTED limit — ``missing_turn_durations`` on a window inside the
  collection era (turns recorded without their durations is a live recorder
  gap, not history) — additionally opens one alert instance per
  (agent, family, condition) episode in the existing alerts store
  (``source="inspect-metrics"``, severity ``warning``), through the same
  helpers every other writer uses (upsert, SSE publish, IM gated by
  ``alerts.im_notify_enabled``).

Both episode edges DERIVE FROM THE STORE, never from an in-process map: every
evaluated call reads this agent's open instances (one bounded query) and
resolves each whose condition the read no longer shows, so a gateway restart
cannot strand an instance the next read already sees cleared; a re-fire
reuses the stored instance instead of minting a duplicate (review finding on
#2790; the sibling writers derive their resolve edges from the store the same
way).

Best-effort by contract: this runs on the statistics read path — a DB, IM or
SSE hiccup must never fail the read; every emitter swallows and logs.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from psycopg.rows import dict_row

from gateway.schemas.inspect_metrics import InspectMetricsMetadata
from shared.alerts import (
    display_language,
    notify_im,
    notify_text,
    stamp_notified,
    upsert_alert,
)
from shared.alerts import fingerprint as alert_fingerprint
from shared.cluster import home_label
from shared.config import settings
from shared.db_transaction import write_transaction
from shared.log import logger
from shared.paths import ava_home

_ALERT_NAME = "inspect metrics coverage"
_FAMILIES = ("cost", "turns", "activity", "lifecycle")
# Expected by construction — log only (see the module docstring).
_EXPECTED = frozenset(
    {
        "historical_coverage_unknown",
        "archive_precision_unattributed",
    }
)

_lock = threading.Lock()
# (agent, family, condition) -> monotonic seconds of the last logged emission.
_last_logged: dict[tuple[int, str, str], float] = {}


def _cooldown_seconds() -> float:
    """Read-path emission cooldown (settings-backed; see AlertsSettings)."""
    return float(settings.alerts.inspect_metrics_degraded_cooldown_seconds)


def _historical(metadata: InspectMetricsMetadata, *, spawned_at: datetime) -> bool:
    """Mirror of the read model's ``historical`` flag (window reaches before
    the observation collection started). A missing window start reads
    historical — the quiet side."""
    if metadata.window_start is None:
        return True
    return max(metadata.window_start, spawned_at) < metadata.collection_started_at


def _conditions(
    metadata: InspectMetricsMetadata,
) -> list[tuple[str, str, str]]:
    """(family, condition, availability) triples for one read."""
    out: list[tuple[str, str, str]] = []
    for family in _FAMILIES:
        ev = getattr(metadata, family)
        if ev.availability != "observed" or ev.retained_unapplied_sources:
            if ev.reason == "missing_turn_durations":
                condition = "missing_turn_durations"
            elif ev.reason == "archive_precision_unattributed" or ev.retained_unapplied_sources:
                condition = "archive_precision_unattributed"
            else:
                condition = "historical_coverage_unknown"
            out.append((family, condition, ev.availability))
    return out


def _unexpected(condition: str, *, historical: bool) -> bool:
    return condition == "missing_turn_durations" and not historical


def _alert_labels(agent_id: int, family: str, condition: str) -> dict[str, str]:
    return {
        "alertname": _ALERT_NAME,
        "agent_id": str(agent_id),
        "family": family,
        "condition": condition,
    }


def _unresolved_start(conn: Any, fp: str) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT starts_at FROM alerts WHERE fingerprint = %s AND status = 'unresolved' "
            "ORDER BY starts_at DESC LIMIT 1",
            (fp,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _open_episode_keys(pool: Any, agent_id: int) -> set[tuple[int, str, str]]:
    """The episode keys this agent's store still holds open.

    Resolve edges derive from the store, never an in-process map: after a
    gateway restart the map would be cold while the rows are still open, and
    a read that shows the condition gone must still close them (review
    finding on #2790). One bounded read. Rows that fail to decode a
    family/condition pair are left untouched — this reconcile only ever
    closes what it fully understands.
    """
    with pool.connection(timeout=1.0) as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT labels FROM alerts WHERE source = 'inspect-metrics'"
            " AND status = 'unresolved' AND labels->>'agent_id' = %s",
            (str(agent_id),),
        )
        rows = cur.fetchall()
    keys: set[tuple[int, str, str]] = set()
    for row in rows:
        labels: dict[str, Any] = row["labels"] or {}
        family = str(labels.get("family") or "")
        condition = str(labels.get("condition") or "")
        if family and condition:
            keys.add((agent_id, family, condition))
    return keys


def _emit_episode(pool: Any, agent_id: int, family: str, condition: str, *, firing: bool) -> None:
    """One firing/resolved edge through the standard alerts machinery.

    The episode identity is (agent, family, condition) — the alert
    fingerprint — and its starts_at comes from the store: an unresolved row
    is reused (dedup), a resolved or absent one starts now. A resolve edge
    with nothing open is a no-op.
    """
    labels = _alert_labels(agent_id, family, condition)
    fp = alert_fingerprint(labels)
    stamp = f"[{home_label(ava_home())}]"
    state = "limited by" if firing else "recovered from"
    summary = f"{stamp} inspector metrics: agent {agent_id} {family} {state} {condition}"
    with write_transaction(pool) as conn:
        starts_at = _unresolved_start(conn, fp)
        if starts_at is None:
            if not firing:
                return  # nothing open — a resolve edge has no one to tell
            starts_at = datetime.now(UTC)
        alert = {
            "status": "firing" if firing else "resolved",
            "labels": labels,
            "annotations": {"summary": summary},
            "starts_at": starts_at.isoformat(),
            "ends_at": "" if firing else datetime.now(UTC).isoformat(),
        }
        alert_key, _did_insert, should_notify, row = upsert_alert(
            conn, alert, source="inspect-metrics"
        )
        lang = display_language(conn)
        text = notify_text(alert, lang) if should_notify else ""
    if not row:
        return
    # SSE publish + IM are best-effort tails — same split as the ingest funnel
    # (gateway/routers/alerts.py): row first, then the live/notification side.
    try:
        from gateway.routers.alerts import publish_alert_rows

        publish_alert_rows([row])
    except Exception:
        logger.warning("inspect metrics coverage: SSE publish failed", agent_id=agent_id)
    if should_notify and text:
        try:
            with write_transaction(pool) as conn:
                if notify_im(text):
                    stamp_notified(conn, [alert_key])
        except Exception:
            logger.warning("inspect metrics coverage: IM notify failed", agent_id=agent_id)


def note_inspect_metrics_coverage(
    pool: Any,
    agent_id: int,
    metadata: InspectMetricsMetadata,
    *,
    spawned_at: datetime,
) -> None:
    """Log every coverage limit (cooldown-deduped) and alert the unexpected
    ones; resolve every open episode whose condition this read no longer
    shows (store-derived — see _open_episode_keys). Never raises — see the
    module docstring."""
    try:
        historical = _historical(metadata, spawned_at=spawned_at)
        conditions = _conditions(metadata)
        present = {(agent_id, family, condition) for family, condition, _ in conditions}
        now = time.monotonic()
        cooldown = _cooldown_seconds()
        for family, condition, availability in conditions:
            key = (agent_id, family, condition)
            with _lock:
                last = _last_logged.get(key)
                due = last is None or now - last >= cooldown
                if due:
                    _last_logged[key] = now
            if not due:
                continue
            logger.warning(
                "inspect metrics coverage limit",
                agent_id=agent_id,
                family=family,
                condition=condition,
                availability=availability,
                expected=condition in _EXPECTED,
            )
            if _unexpected(condition, historical=historical):
                _emit_episode(pool, agent_id, family, condition, firing=True)
        # Conditions this read no longer shows cannot still be firing — close
        # every open instance for them. The open set comes from the store
        # (not an in-process map), so a cold process resolves them too.
        for key in _open_episode_keys(pool, agent_id) - present:
            _emit_episode(pool, agent_id, key[1], key[2], firing=False)
    except Exception:
        logger.exception("inspect metrics coverage note failed", agent_id=agent_id)
