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
  (``historical_coverage_unknown``), an agent without a compact boundary
  (``compact_boundary_unknown``), a legacy archive whose per-day precision
  cannot be attributed per turn (``archive_precision_unattributed``, incl.
  retained sources). Alerting them would only move the panel's chronic noise
  into the alerts surface — the same ruling grades expected gaps down.
- an UNEXPECTED limit — ``missing_turn_durations`` on a window inside the
  collection era (turns recorded without their durations is a live recorder
  gap, not history) — additionally opens one alert instance per
  (agent, family, condition) episode in the existing alerts store
  (``source="inspect-metrics"``, severity ``warning``), through the same
  helpers every other writer uses (upsert, SSE publish, IM gated by
  ``alerts.im_notify_enabled``). A later read that no longer shows the
  condition resolves the instance. The episode key reuses the store's own
  unresolved row when the in-process map is cold (a gateway restart must not
  orphan an instance).

Best-effort by contract: this runs on the statistics read path — a DB, IM or
SSE hiccup must never fail the read; every emitter swallows and logs.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

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
        "compact_boundary_unknown",
        "archive_precision_unattributed",
    }
)

_lock = threading.Lock()
# (agent, family, condition) -> monotonic seconds of the last logged emission.
_last_logged: dict[tuple[int, str, str], float] = {}
# Open unexpected-episode map: (agent, family, condition) -> starts_at.
_episodes: dict[tuple[int, str, str], datetime] = {}


def _cooldown_seconds() -> float:
    """Read-path emission cooldown (settings-backed; see AlertsSettings)."""
    return float(settings.alerts.inspect_metrics_degraded_cooldown_seconds)


def _historical(metadata: InspectMetricsMetadata, *, spawned_at: datetime) -> bool:
    """Mirror of the read model's ``historical`` flag (window reaches before
    the observation collection started). A missing window start reads
    historical — the quiet side, and the only shape it appears in
    (``compact_boundary_unknown``) never reaches the unexpected set."""
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
            elif ev.reason == "compact_boundary_unknown":
                condition = "compact_boundary_unknown"
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


def _emit_episode(pool: Any, agent_id: int, family: str, condition: str, *, firing: bool) -> None:
    """One firing/resolved edge through the standard alerts machinery."""
    labels = _alert_labels(agent_id, family, condition)
    fp = alert_fingerprint(labels)
    key = (agent_id, family, condition)
    stamp = f"[{home_label(ava_home())}]"
    state = "limited by" if firing else "recovered from"
    summary = f"{stamp} inspector metrics: agent {agent_id} {family} {state} {condition}"
    with write_transaction(pool) as conn:
        starts_at: datetime | None = None
        if firing:
            with _lock:
                starts_at = _episodes.get(key)
            if starts_at is None:
                starts_at = _unresolved_start(conn, fp)
            if starts_at is None:
                starts_at = datetime.now(UTC)
        else:
            with _lock:
                starts_at = _episodes.pop(key, None)
            if starts_at is None:
                starts_at = _unresolved_start(conn, fp)
            if starts_at is None:
                return  # nothing open — a resolve edge has no one to tell
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
        if firing:
            with _lock:
                _episodes[key] = alert_key[1]
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
    ones; resolve episodes whose condition a later read no longer shows.
    Never raises — see the module docstring."""
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
        # A condition the store still holds open but this read no longer shows
        # has cleared — close its episode (per evaluated agent only).
        with _lock:
            stale = [k for k in _episodes if k[0] == agent_id and k not in present]
        for key in stale:
            _emit_episode(pool, agent_id, key[1], key[2], firing=False)
    except Exception:
        logger.exception("inspect metrics coverage note failed", agent_id=agent_id)
