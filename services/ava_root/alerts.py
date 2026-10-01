"""Root unit alert episodes: the durable store behind firing and resolving.

The health monitor derives one condition per unit each round — the unit's
intent is running and it sits in an explicit failure state: a recorded
replacement failure, an open restart breaker, or retained native custody.
This module turns that derived condition into an episode with a durable
identity (task #4872, the B route):

- fire once per episode: the record is written before any fan-out, so a crash
  mid-fan-out can never re-fire;
- hold while the condition stays present — backoff rounds never re-fire, and
  a change of failure kind updates the open record in place;
- resolve when the condition disappears, replaying the episode identity; the
  record is cleared first, so the condition returning afterwards starts a
  fresh episode instead of hiding behind the old record.

The store survives a root restart: an episode still present afterwards never
re-fires, and one whose condition disappeared while root was down still
resolves. Deployment wiring supplies the user-channel notifier; without one
the registered events remain the only fan-out.

Store format (`<run_dir>/alerts/<unit>.json`, times in epoch seconds; atomic,
best-effort writes — an unwritable store never fails the observation round):

    {"kind": "restart_failed"|"breaker_open"|"custody_held",
     "since": <epoch>, "fired_at": <epoch>, "detail": str,
     "delivered_at": <epoch>|null}
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from time import time
from typing import Protocol, cast

from services.ava_root.intent_store import RestartFailure

_log = logging.getLogger(__name__)

_DIRECTORY = "alerts"
_RECORD_FIELDS = frozenset({"kind", "since", "fired_at", "detail", "delivered_at"})

# Delivery outcomes as the events record them.
_POSTED = "posted"
_FAILED = "failed"
_SKIPPED = "skipped"

ALERTNAME = "root-unit-alert"
"""The one rule name every root-unit alert instance carries."""


class AlertKind(StrEnum):
    """The alertable failure states a unit can sit in (derivation order below)."""

    RESTART_FAILED = "restart_failed"
    """A replacement failed at its down or up half; the monitor retries it."""

    BREAKER_OPEN = "breaker_open"
    """Repeated non-alive rounds opened the restart breaker; restarts are held."""

    CUSTODY_HELD = "custody_held"
    """Retained native custody blocks revival until reconciliation."""


# The severity each failure state carries (ladder: warning < error < critical).
# A retrying replacement failure reports at `error`; an exhausted breaker at
# `critical` (restarts are held until an operator acts); retained custody at
# `warning` (blocked until reconciliation, no operator action implied).
_SEVERITY: dict[AlertKind, str] = {
    AlertKind.RESTART_FAILED: "error",
    AlertKind.BREAKER_OPEN: "critical",
    AlertKind.CUSTODY_HELD: "warning",
}


@dataclass(frozen=True, slots=True)
class EpisodeRecord:
    """One fired episode — the durable identity a resolve replays."""

    kind: AlertKind
    since: float
    """Episode start (epoch seconds): the instance key with the fingerprint."""
    fired_at: float
    detail: str
    delivered_at: float | None
    """When the user-channel post for this episode was last accepted, if ever."""


@dataclass(frozen=True, slots=True)
class UnitAlertFacts:
    """The supervisor-side facts of one unit (`Supervisor.unit_alert_facts`)."""

    intent_running: bool
    restart_failed: RestartFailure | None
    custody_held: bool


@dataclass(frozen=True, slots=True)
class UnitAlertView:
    """One unit as the health monitor observed it this round."""

    unit: str
    facts: UnitAlertFacts
    breaker_open: bool
    detail: str = ""


class AlertNotifier(Protocol):
    """The user-channel seam: one post per fire / resolve edge, True when accepted."""

    def notify(self, unit: str, record: EpisodeRecord, *, resolved: bool) -> bool:
        """Post one episode edge and answer whether the channel accepted it."""
        ...


def derive_kind(view: UnitAlertView) -> AlertKind | None:
    """The unit's alertable failure state, or None while it is not alerting.

    Only a unit whose intent is running can alert; among its failure states,
    the recorded replacement failure wins over the open breaker, which wins
    over retained custody.
    """
    if not view.facts.intent_running:
        return None
    if view.facts.restart_failed is not None:
        return AlertKind.RESTART_FAILED
    if view.breaker_open:
        return AlertKind.BREAKER_OPEN
    if view.facts.custody_held:
        return AlertKind.CUSTODY_HELD
    return None


def describe(view: UnitAlertView, kind: AlertKind) -> str:
    """The episode's one-line evidence for `kind`, from the view's facts."""
    failure = view.facts.restart_failed
    if kind is AlertKind.RESTART_FAILED and failure is not None:
        return f"replacement failed at its {failure.stage.value} half: {failure.detail}"
    if kind is AlertKind.BREAKER_OPEN:
        return view.detail or "restart breaker open"
    if kind is AlertKind.CUSTODY_HELD:
        return "native custody requires reconciliation"
    return view.detail


# -- the episode store ----------------------------------------------------------


def write_record(run_dir: Path, unit: str, record: EpisodeRecord) -> None:
    """Persist one unit's episode atomically; failures are logged, never raised."""
    from base.host.atomic_io import write_text_atomic

    try:
        directory = run_dir / _DIRECTORY
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_text_atomic(directory / f"{unit}.json", _encode(record), mode=0o600, sync_parent=True)
    except OSError as exc:
        _log.error("unit %s: alert episode write failed: %s", unit, exc)


def read_record(run_dir: Path, unit: str) -> EpisodeRecord | None:
    """Read one unit's episode; absent or unreadable yields None (logged)."""
    path = run_dir / _DIRECTORY / f"{unit}.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _log.warning("unit %s: alert episode unreadable (%s); treated as absent", unit, exc)
        return None
    try:
        return _decode(json.loads(text))
    except (ValueError, TypeError) as exc:
        _log.warning("unit %s: alert episode invalid (%s); treated as absent", unit, exc)
        return None


def clear_record(run_dir: Path, unit: str) -> None:
    """Remove one unit's episode; a failure is logged, never raised."""
    try:
        (run_dir / _DIRECTORY / f"{unit}.json").unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        _log.error("unit %s: alert episode clear failed: %s", unit, exc)


def _encode(record: EpisodeRecord) -> str:
    body: dict[str, object] = {
        "kind": record.kind.value,
        "since": record.since,
        "fired_at": record.fired_at,
        "detail": record.detail,
        "delivered_at": record.delivered_at,
    }
    return json.dumps(body, separators=(",", ":"), sort_keys=True) + "\n"


def _decode(raw: object) -> EpisodeRecord:
    if not isinstance(raw, dict):
        raise TypeError("record must be an object")
    values = cast("Mapping[str, object]", raw)
    if frozenset(values) != _RECORD_FIELDS:
        raise ValueError(f"record must contain exactly {sorted(_RECORD_FIELDS)}")
    delivered = values["delivered_at"]
    return EpisodeRecord(
        kind=_member(AlertKind, values["kind"], "kind"),
        since=_timestamp(values["since"], "since"),
        fired_at=_timestamp(values["fired_at"], "fired_at"),
        detail=_text(values["detail"], "detail"),
        delivered_at=None if delivered is None else _timestamp(delivered, "delivered_at"),
    )


def _timestamp(raw: object, field: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"{field} must be a number")
    return float(raw)


def _text(raw: object, field: str) -> str:
    if not isinstance(raw, str):
        raise TypeError(f"{field} must be a string")
    return raw


def _member[E: StrEnum](enum_type: type[E], raw: object, field: str) -> E:
    if not isinstance(raw, str):
        raise TypeError(f"{field} must be a string")
    try:
        return enum_type(raw)
    except ValueError as exc:
        choices = [item.value for item in enum_type]
        raise ValueError(f"{field} {raw!r} is not one of {choices}") from exc


# -- the router -----------------------------------------------------------------


class AlertRouter:
    """Derive one observation round into fire / hold / resolve transitions.

    The record is written before any fan-out (the registered event, the
    user-channel post), so a crash mid-fan-out can never re-fire an episode;
    the resolve clears the record before its fan-out, so a condition that
    returns after a partly-failed resolve starts a fresh episode instead of
    hiding behind the old record.

    Failures stay inside one unit: a store or notifier fault is logged and
    the rest of the round proceeds — alerting is a side channel and must
    never break health observation.
    """

    def __init__(self, run_dir: Path, *, notifier: AlertNotifier | None = None) -> None:
        self._run_dir = run_dir
        self._notifier = notifier

    def observe(self, views: Iterable[UnitAlertView]) -> None:
        """One round over every unit view, in order; per-unit isolation."""
        for view in views:
            try:
                self._observe_unit(view)
            except Exception:
                _log.exception("unit %s: alert observation failed; continuing", view.unit)

    def _observe_unit(self, view: UnitAlertView) -> None:
        kind = derive_kind(view)
        record = read_record(self._run_dir, view.unit)
        if kind is None:
            if record is not None:
                self._resolve(view.unit, record)
            return
        if record is None:
            self._fire(view, kind)
        elif record.kind is not kind:
            self._update_kind(view.unit, record, kind, describe(view, kind))

    def _fire(self, view: UnitAlertView, kind: AlertKind) -> None:
        now = time()
        record = EpisodeRecord(
            kind=kind, since=now, fired_at=now, detail=describe(view, kind), delivered_at=None
        )
        write_record(self._run_dir, view.unit, record)
        delivery = self._deliver(view.unit, record, resolved=False)
        if delivery == _POSTED:
            write_record(self._run_dir, view.unit, replace(record, delivered_at=time()))
        _emit_fired(view.unit, record, delivery)

    def _update_kind(self, unit: str, record: EpisodeRecord, kind: AlertKind, detail: str) -> None:
        """The open episode's failure kind moved; keep its identity, no re-fire."""
        write_record(self._run_dir, unit, replace(record, kind=kind, detail=detail))
        _log.info(
            "unit %s: alert episode stays open; failure state moved %s -> %s (no re-fire)",
            unit,
            record.kind.value,
            kind.value,
        )

    def _resolve(self, unit: str, record: EpisodeRecord) -> None:
        clear_record(self._run_dir, unit)
        if record.delivered_at is None:
            # The firing never reached the channel; posting a resolution would
            # fabricate a row for an alert nobody saw.
            delivery = _SKIPPED
        else:
            delivery = self._deliver(unit, record, resolved=True)
        _emit_resolved(unit, record, delivery)

    def _deliver(self, unit: str, record: EpisodeRecord, *, resolved: bool) -> str:
        """One user-channel post; answers "posted", "failed", or "skipped"."""
        if self._notifier is None:
            return _SKIPPED
        try:
            accepted = self._notifier.notify(unit, record, resolved=resolved)
        except Exception:
            _log.exception("unit %s: alert notifier raised; recording a failed delivery", unit)
            return _FAILED
        return _POSTED if accepted else _FAILED


# -- the reference notifier -----------------------------------------------------


class AlertWebhookNotifier:
    """The reference user-channel post: one `/api/alerts` call per episode edge.

    Reuses the health probe's client posture: `gateway_api_base()` for the
    URL, `gateway_auth_headers()` for the bearer, plus `X-Alerts-Token` when
    this home carries the cluster webhook token. A failed post is retried
    once, then given up — the outcome lands in the event stream, and the
    store stays authoritative either way.

    Transport bounds (written reasons, protocol class): one post is bounded by
    `timeout_s` so an unreachable gateway cannot stall the caller for longer
    than a bounded attempt (the call site runs off the root event loop), and
    the retry is capped at one extra attempt — "retry once, then drop":
    alerting is a side channel, and a channel needing more than two tries is
    itself the incident.
    """

    def __init__(self, *, timeout_s: float = 10.0, attempts: int = 2) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        self._timeout_s = timeout_s
        self._attempts = attempts

    def notify(self, unit: str, record: EpisodeRecord, *, resolved: bool) -> bool:
        """Post one episode edge; True means the gateway accepted the row."""
        import httpx

        payload = _webhook_payload(unit, record, resolved=resolved)
        try:
            from base.cluster.machine import gateway_api_base, gateway_auth_headers

            url = f"{gateway_api_base()}/api/alerts"
            headers = dict(gateway_auth_headers())
            headers.update(_alerts_token_headers())
        except Exception as exc:
            _log.warning(
                "unit %s: root alert post skipped — gateway endpoint or token unavailable (%s)",
                unit,
                type(exc).__name__,
            )
            return False
        for attempt in range(1, self._attempts + 1):
            try:
                response = httpx.post(url, json=payload, headers=headers, timeout=self._timeout_s)
                response.raise_for_status()
                return True
            except Exception as exc:
                if attempt >= self._attempts:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    _log.warning(
                        "unit %s: root alert post failed after %d attempt(s): %s%s",
                        unit,
                        attempt,
                        type(exc).__name__,
                        f" (HTTP {status})" if status is not None else "",
                    )
        return False


def _webhook_payload(unit: str, record: EpisodeRecord, *, resolved: bool) -> dict[str, object]:
    """The Alertmanager-shaped POST body for one episode edge.

    Wire keys are camelCase (`startsAt`/`endsAt`): the ingest schema is
    alias-only with `extra=ignore`, so a snake_case key is silently dropped
    and the whole row rejected (task #3726).

    `fingerprint` is computed over the stable identity labels only
    (`alertname`, `unit`) — the health probe's posture: severity and kind may
    change while an episode stays open, and the (fingerprint, startsAt) dedup
    key must hold across that change and across the resolve replay.
    """
    from base.telemetry.alerts import fingerprint

    labels = {
        "alertname": ALERTNAME,
        "severity": _SEVERITY[record.kind],
        "unit": unit,
        "kind": record.kind.value,
    }
    summary = (
        f"root unit {unit}: recovered from {record.kind.value}"
        if resolved
        else f"root unit {unit}: {record.kind.value} ({record.detail})"
    )
    return {
        "source": "ava-root",
        "alerts": [
            {
                "status": "resolved" if resolved else "firing",
                "labels": labels,
                "annotations": {"summary": summary},
                "startsAt": _iso(record.since),
                "endsAt": _iso(time()) if resolved else "",
                "fingerprint": fingerprint({"alertname": ALERTNAME, "unit": unit}),
            }
        ],
    }


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _alerts_token_headers() -> dict[str, str]:
    """`X-Alerts-Token` when this home carries the cluster webhook token.

    The token authenticates the ingest route; homes without one (a
    credential-free remote runner) still present their machine bearer, and a
    tokenless single box falls back to the route's loopback trust.
    """
    from base.config import settings

    token = settings.alerts.webhook_token
    return {} if token is None else {"X-Alerts-Token": token.get_secret_value()}


# -- registered events ----------------------------------------------------------


def _emit_fired(unit: str, record: EpisodeRecord, delivery: str) -> None:
    try:
        from base.log import logger

        logger.warning(
            "unit {unit}: alert episode fired ({kind}) — intent is running and the unit sits "
            "in an explicit failure state; the episode holds until the condition clears",
            event="root_unit_alert_fired",
            unit=unit,
            kind=record.kind.value,
            since_timestamp_seconds=record.since,
            detail=record.detail,
            delivery=delivery,
        )
    except Exception:
        # Fan-out may lag or fail; the record is already stored, so never let
        # a signal failure corrupt the transition that produced it.
        _log.exception("unit %s: alert-fired event fan-out failed", unit)


def _emit_resolved(unit: str, record: EpisodeRecord, delivery: str) -> None:
    try:
        from base.log import logger

        logger.info(
            "unit {unit}: alert episode resolved ({kind}) after {failed_for_s:.0f}s",
            event="root_unit_alert_resolved",
            unit=unit,
            kind=record.kind.value,
            since_timestamp_seconds=record.since,
            failed_for_s=max(0.0, time() - record.since),
            delivery=delivery,
        )
    except Exception:
        _log.exception("unit %s: alert-resolved event fan-out failed", unit)
