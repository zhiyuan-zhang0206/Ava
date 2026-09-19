"""Deferred-delivery outbox: never silently drop a message the wire refused.

A chat send that exhausts its retry budget (the SDK's 3 attempts, a caller's
own bounded retry chain) reaches the end of its attempts with the message still
in nobody's hands: no inbound row was ever committed, no server-side record
exists, and the failure is visible only in the caller's log (the 2026-09-17
black-window evidence, task #3757: "fire != delivered"). This module closes
that window on the sending machine:

- **Record (dead-hand coverage).** When a delivery POST finally fails, the SDK
  (or the `ava agents send` CLI) calls `record_failed_send`. The message is written durably to
  ``$AVA_HOME/state/delivery-outbox/`` — one JSON file per logical message —
  BEFORE the send raises, so the record does not depend on the caller's process
  surviving or retrying.
- **Redeliver (bounded, delayed).** The machine's ops daemon runs the flush loop
  (`flush`), which re-commits each due entry through the canonical
  `insert_chat_inbound_once` path — the same durable INSERT + wake the HTTP
  route uses — so every downstream mechanism (delivery watchdog dispatch,
  claim recheck, terminated-owner resurrect retry) completes the delivery.
  Retries follow a configurable backoff ladder; the first failed attempt at
  or after the configurable budget abandons the entry, so an attempt is never
  skipped on time alone.
- **Escalate (keep it loud).** A delivery that cannot land within the budget,
  or that fails permanently (missing agent, key conflict), is abandoned with a
  WARNING, a `delivery_outbox_abandoned` telemetry event, and the record kept
  on disk (marked `abandoned`, carrying its stable reason code and the readable
  failure detail where one exists) instead of vanishing. The inspection window is
  bounded: the flush pass expires abandoned records
  `delivery_outbox_abandoned_retention_days` (30d default) after abandonment.

**Exactness.** All attempts and the flush share ONE idempotency key per logical
message: `logical_key` reuses the pending key for the same
(machine, target, source, content) fingerprint within the dedup window, so a
sender retry chain (reference watchers: 8 attempts over ~10.5 min, task #3694)
journals one entry, and whichever attempt — or the flush — commits first wins;
every later attempt and the flush then hit the `client_message_id` receipt and
no-op. Consumer impact of the residual at-least-once cases (process death
between attempts; two identical messages inside the window while both
undelivered; a delivery that landed while its record write raced) is a repeated
chat message, never a lost one — the same failure mode caller-level retries
already carry, now bounded by the dedup window instead of unbounded.

Boundary: this module is the *server-side* half of the delivery contract. The
caller-side retry budgets (#3525 / #3694 template contracts) keep doing what
they do; the outbox only starts once those have run out. Template code and the
notices/`system-note` surfaces are untouched (see the PR design section).

Kill switch: `delivery_outbox_enabled` is read live by the flusher (a flip
stops redelivery within one tick); a sender process picks it up at its next
start. Records are never deleted by the switch — an off outbox is inert, not
lossy.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from shared.log import logger
from shared.paths import ava_home
from shared.turn_identity import effective_agent_id

_ENTRY_SCHEMA = 1
_ENTRY_SUFFIX = ".json"

# Content type the SDK accepts: a plain string or OpenAI-shaped blocks.
Content = str | list[dict[str, object]]

# HTTP statuses that mean "the gateway or one of its backends hiccuped" — a
# delivery attempt worth replaying once the backend returns. 500 = unhandled
# server error (the 2026-08-07 memory-indexer 500 class), 502/503 = a backend
# (indexer / cross-machine runner) is down, 504 = gateway-side timeout,
# 429 = rate-limited. 4xx are NOT here: the wire `reason` is authoritative
# application semantics (AgentNotFound etc.); replaying cannot change the
# result. One definition, shared by the SDK transport's retry policy
# (`ava/_gateway_transport.py`) and the outbox interception on both send paths
# (SDK `send_message` and the `ava agents send` CLI).
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class DeliveryOutboxLimits:
    """The delivery-outbox knobs, read as one snapshot."""

    enabled: bool
    retry_backoff_steps: tuple[float, ...]
    budget_seconds: float
    abandoned_retention_days: int
    dedup_window_seconds: float
    flush_interval_seconds: float
    max_entries: int


def limits() -> DeliveryOutboxLimits:
    """Read the outbox knobs through the live config path (`.env` file primary).

    One `current_field_values()` read costs tens of milliseconds (the dotenv
    resolution dominates), so callers on hot paths cache the pair they need —
    see `send_path_settings` (sender processes) — while the flush tick and the
    failure recorder read fresh.
    """
    from shared.config.service_read import current_field_values

    values = current_field_values()
    return DeliveryOutboxLimits(
        enabled=bool(values["delivery_outbox_enabled"]),
        retry_backoff_steps=tuple(
            float(step) for step in values["delivery_outbox_retry_backoff_steps_s"]
        ),
        budget_seconds=float(values["delivery_outbox_budget_seconds"]),
        abandoned_retention_days=int(values["delivery_outbox_abandoned_retention_days"]),
        dedup_window_seconds=float(values["delivery_outbox_dedup_window_seconds"]),
        flush_interval_seconds=float(values["delivery_outbox_flush_interval_seconds"]),
        max_entries=int(values["delivery_outbox_max_entries"]),
    )


_send_path_cache: tuple[bool, float] | None = None
_send_path_lock = threading.Lock()


def send_path_settings() -> tuple[bool, float]:
    """`(enabled, dedup_window)` for sender processes — read once per process.

    A delivery send must not pay a config read it cannot use: the sender side
    of the switch (recording, key reuse) lands at the next process start, while
    the flusher half stays live per tick. That is the repo's standard
    field-application semantics, and it is what makes the kill switch cheap.
    """
    global _send_path_cache  # noqa: PLW0603 — lazily-filled process cache
    with _send_path_lock:
        if _send_path_cache is None:
            snapshot = limits()
            _send_path_cache = (snapshot.enabled, snapshot.dedup_window_seconds)
        return _send_path_cache


def _reset_caches_for_tests() -> None:
    global _send_path_cache  # noqa: PLW0603
    with _send_path_lock:
        _send_path_cache = None
    with _registry_lock:
        _registry.clear()


# ── Journal layout ───────────────────────────────────────────────────────────


def journal_dir() -> Path:
    """The durable record directory; creation is deferred to the first write."""
    return ava_home() / "state" / "delivery-outbox"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _parse_iso(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"delivery outbox timestamp lacks tzinfo: {raw!r}")
    return parsed


def _canonical_content(content: Content) -> str:
    if isinstance(content, str):
        return "s:" + content
    return "j:" + json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(agent_id: int, source: str, content: Content) -> str:
    """Identity of one logical message: target + source + exact content."""
    raw = f"{agent_id}\x1f{source}\x1f{_canonical_content(content)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def split_content(content: Content) -> tuple[str, dict[str, object] | None]:
    """Map wire content to its durable `(content, payload)` form.

    The semantic twin of `gateway/routers/agents_state.py::_normalize_message_content`
    (kept here because `services` may not import `gateway`): a string is
    stripped; a block list stores the joined text part (or `"[image]"`) plus the
    `{"content_blocks": [...]}` payload the claim node inlines natively.

    The strip mirrors the wire model: `ops/rpc_schemas._MessageContent` parses
    string content with `strip_whitespace=True, min_length=1`, so the route's
    stored row carries the stripped form. A flushed insert bypasses that model,
    and `shared/chat_delivery._matching_receipt` compares stored vs incoming
    content exactly — an unstripped replay of a whitespace-edged string would
    read as a different message (a false `key_conflict` / 409) although it is
    the same one.
    """
    if isinstance(content, str):
        return content.strip(), None
    texts: list[str] = []
    for block in content:
        raw = block.get("text")
        if block.get("type") == "text" and isinstance(raw, str) and raw.strip():
            texts.append(raw)
    return "\n".join(texts) or "[image]", {"content_blocks": [dict(block) for block in content]}


@dataclass(frozen=True)
class OutboxEntry:
    """One durable deferred delivery, as stored on disk."""

    schema_version: int
    agent_id: int
    source: str
    content: Content
    client_message_id: str
    created_at: str
    last_attempt_at: str
    attempts: int
    origin_agent_id: int | None
    origin_pid: int | None
    flush_attempts: int
    last_flush_at: str | None
    state: Literal["pending", "abandoned"]
    abandon_reason: str | None
    abandon_detail: str | None
    abandoned_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "agent_id": self.agent_id,
            "source": self.source,
            "content": self.content,
            "client_message_id": self.client_message_id,
            "created_at": self.created_at,
            "last_attempt_at": self.last_attempt_at,
            "attempts": self.attempts,
            "origin_agent_id": self.origin_agent_id,
            "origin_pid": self.origin_pid,
            "flush_attempts": self.flush_attempts,
            "last_flush_at": self.last_flush_at,
            "state": self.state,
            "abandon_reason": self.abandon_reason,
            "abandon_detail": self.abandon_detail,
            "abandoned_at": self.abandoned_at,
        }


def _entry_path_name(agent_id: int, message_fingerprint: str, stamp: datetime) -> str:
    return f"{agent_id}_{message_fingerprint}_{int(stamp.timestamp() * 1000)}{_ENTRY_SUFFIX}"


def _write_atomic(path: Path, entry: OutboxEntry) -> None:
    """Write one entry durably (tmp + fsync + replace), mirroring the
    pty-close-notices journal discipline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(entry.as_dict(), stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        Path(raw_tmp).replace(path)
        if os.name != "nt":
            fd_dir = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd_dir)
            finally:
                os.close(fd_dir)
    except BaseException:
        with suppress(OSError):
            Path(raw_tmp).unlink()
        raise


def _read(path: Path) -> OutboxEntry | None:
    """Parse one record; None for unreadable/drifted files (kept for inspection).

    A record whose timestamps do not parse — or that lack a timezone — is
    unreadable too, and is rejected HERE: every consumer (the flush loop, the
    retry ladder, the retention predicate, the fingerprint-merge scan) subtracts
    these fields inside its own per-record handling, so a raise from there would
    abort the whole pass — one corrupt record would wedge every later one.
    """
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return None
        raw = cast("dict[str, object]", raw)
        if raw.get("schema_version") != _ENTRY_SCHEMA:
            return None
        state = raw.get("state")
        if state not in ("pending", "abandoned"):
            return None
        content = raw.get("content")
        if not isinstance(content, str) and not isinstance(content, list):
            return None
        entry = OutboxEntry(
            schema_version=_ENTRY_SCHEMA,
            agent_id=int(cast("int", raw["agent_id"])),
            source=cast("str", raw["source"]),
            content=cast("Content", content),
            client_message_id=cast("str", raw["client_message_id"]),
            created_at=cast("str", raw["created_at"]),
            last_attempt_at=cast("str", raw["last_attempt_at"]),
            attempts=int(cast("int", raw["attempts"])),
            origin_agent_id=cast("int | None", raw.get("origin_agent_id")),
            origin_pid=cast("int | None", raw.get("origin_pid")),
            flush_attempts=int(cast("int", raw.get("flush_attempts", 0))),
            last_flush_at=cast("str | None", raw.get("last_flush_at")),
            state=state,
            abandon_reason=cast("str | None", raw.get("abandon_reason")),
            abandon_detail=cast("str | None", raw.get("abandon_detail")),
            abandoned_at=cast("str | None", raw.get("abandoned_at")),
        )
        # Timestamps must parse (and carry a timezone) before anything consumes
        # them — see the docstring.
        created = _parse_iso(entry.created_at)
        _parse_iso(entry.last_attempt_at)
        if entry.last_flush_at is not None:
            _parse_iso(entry.last_flush_at)
        if entry.abandoned_at is not None:
            _parse_iso(entry.abandoned_at)
        # The filename carries the fingerprint — a record whose content no longer
        # matches its name was corrupted or hand-edited; never trust it.
        expected = _entry_path_name(
            entry.agent_id,
            fingerprint(entry.agent_id, entry.source, entry.content),
            created,
        )
        if path.name != expected:
            return None
    except (ValueError, KeyError, TypeError, OSError):
        return None
    return entry


def _matching_paths(agent_id: int, message_fingerprint: str) -> list[Path]:
    directory = journal_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{agent_id}_{message_fingerprint}_*{_ENTRY_SUFFIX}"))


# ── Sender side (SDK) ────────────────────────────────────────────────────────

_registry_lock = threading.Lock()
# in-process pending keys: fingerprint -> (idempotency key, last-use monotonic)
_registry: dict[str, tuple[str, float]] = {}


def logical_key(*, agent_id: int, source: str, content: Content) -> str:
    """The idempotency key for this logical message.

    While the same fingerprint retries within the dedup window (and until any
    attempt reports success through `note_send_succeeded`), every attempt
    reuses one key: the server's `client_message_id` receipt then makes the
    whole chain exactly-once, and the outbox entry shares the key. After a
    successful send, an identical later message is a NEW logical message and
    gets a fresh key.
    """
    enabled, window = send_path_settings()
    if not enabled:
        return uuid.uuid4().hex
    message_fingerprint = fingerprint(agent_id, source, content)
    now = time.monotonic()
    with _registry_lock:
        for stale in [fp for fp, (_, at) in _registry.items() if now - at > window]:
            del _registry[stale]
        hit = _registry.get(message_fingerprint)
        if hit is not None:
            _registry[message_fingerprint] = (hit[0], now)
            return hit[0]
        key = uuid.uuid4().hex
        _registry[message_fingerprint] = (key, now)
        return key


def note_send_succeeded(*, agent_id: int, source: str, content: Content, key: str) -> None:
    """One logical message landed: retire its key and any pending record.

    Best-effort and never raises — it runs on the send path's success case and
    must not turn a delivered message into a failed call.
    """
    try:
        message_fingerprint = fingerprint(agent_id, source, content)
        with _registry_lock:
            _registry.pop(message_fingerprint, None)
        for path in _matching_paths(agent_id, message_fingerprint):
            entry = _read(path)
            if entry is not None and entry.state == "pending" and entry.client_message_id == key:
                with suppress(OSError):
                    path.unlink()
                return
    except Exception:
        logger.opt(exception=True).warning(
            "[delivery-outbox] failed to retire the record for a delivered message"
        )


def record_failed_send(
    *,
    agent_id: int,
    source: str,
    content: Content,
    client_message_id: str,
    now: datetime | None = None,
) -> Path | None:
    """Durably record one failed delivery; returns the record path or None.

    Same-fingerprint failures within the dedup window of the entry's last
    attempt merge into the existing pending record (attempts counter folded,
    key advanced to this attempt's), so a retry chain leaves one record, not
    one per attempt. Further-apart identical messages stay separate records.
    Never raises: a failed record must leave the send's own error untouched.
    """
    try:
        snapshot = limits()
        if not snapshot.enabled:
            return None
        moment = now or datetime.now(UTC)
        message_fingerprint = fingerprint(agent_id, source, content)
        for path in _matching_paths(agent_id, message_fingerprint):
            entry = _read(path)
            if entry is None or entry.state != "pending":
                continue
            age = (moment - _parse_iso(entry.last_attempt_at)).total_seconds()
            if age <= snapshot.dedup_window_seconds:
                merged = replace(
                    entry,
                    client_message_id=client_message_id,
                    attempts=entry.attempts + 1,
                    last_attempt_at=_iso(moment),
                )
                _write_atomic(path, merged)
                return path
        pending = sum(
            1
            for path in journal_dir().glob(f"*{_ENTRY_SUFFIX}")
            if (entry := _read(path)) is not None and entry.state == "pending"
        )
        if pending >= snapshot.max_entries:
            logger.warning(
                "[delivery-outbox] entry cap ({} pending) reached on this machine; "
                "the failed send to agent {} is not recorded",
                snapshot.max_entries,
                agent_id,
            )
            return None
        origin = effective_agent_id()
        entry = OutboxEntry(
            schema_version=_ENTRY_SCHEMA,
            agent_id=agent_id,
            source=source,
            content=content,
            client_message_id=client_message_id,
            created_at=_iso(moment),
            last_attempt_at=_iso(moment),
            attempts=1,
            origin_agent_id=origin,
            origin_pid=os.getpid(),
            flush_attempts=0,
            last_flush_at=None,
            state="pending",
            abandon_reason=None,
            abandon_detail=None,
            abandoned_at=None,
        )
        path = journal_dir() / _entry_path_name(agent_id, message_fingerprint, moment)
        _write_atomic(path, entry)
        logger.info(
            "[delivery-outbox] recorded failed delivery to agent {} (source {!r}); "
            "will redeliver when the gateway returns",
            agent_id,
            source,
        )
        return path
    except Exception:
        logger.opt(exception=True).warning(
            "[delivery-outbox] failed to record a failed delivery to agent {}", agent_id
        )
        return None


# ── Flusher side (agent-ops) ─────────────────────────────────────────────────


class FlushPool(Protocol):
    """Structural type of the ops daemon's connection pool.

    Kept psycopg-free so the sender-side import of this module stays light:
    only the flush side (the ops daemon) holds a real pool, and it passes it
    in. `connection` mirrors the psycopg_pool call the flush makes — a bounded
    wait so a down data plane cannot park the flusher past its next tick.
    """

    def connection(self, *, timeout: float | None = None) -> AbstractContextManager[Any]: ...


@dataclass(frozen=True)
class FlushReport:
    """What one flush pass did; drives tests and the daemon's logging."""

    delivered: int = 0
    abandoned: int = 0
    deferred: int = 0
    unreadable: int = 0
    expired: int = 0

    @property
    def touched(self) -> int:
        return self.delivered + self.abandoned


class PermanentDeliveryError(Exception):
    """The record can never be delivered; abandon it with this reason.

    `detail` carries the readable upstream text (the exception that decided the
    refusal, when one exists), so the abandonment record explains its code
    instead of only naming it.
    """

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _deliver(pool: FlushPool, entry: OutboxEntry, connect_timeout_s: float) -> int:
    """Commit one entry through the canonical chat-inbound path; returns the id."""
    from shared.caller_protocol import CallerProtocolUnavailableError
    from shared.chat_delivery import ClientMessageConflictError, insert_chat_inbound_once
    from shared.db import publish_inbound_wake

    text, payload = split_content(entry.content)
    with pool.connection(timeout=connect_timeout_s) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM agents_meta WHERE id = %s", (entry.agent_id,))
        if cur.fetchone() is None:
            # A missing agent row is permanent: ids are never re-assigned.
            raise PermanentDeliveryError("agent_missing")
    try:
        with pool.connection(timeout=connect_timeout_s) as conn:
            receipt = insert_chat_inbound_once(
                conn,
                agent_id=entry.agent_id,
                content=text,
                source=entry.source,
                payload=payload,
                client_message_id=entry.client_message_id,
            )
    except ClientMessageConflictError as exc:
        raise PermanentDeliveryError("key_conflict", detail=str(exc)) from exc
    except CallerProtocolUnavailableError as exc:
        raise PermanentDeliveryError("caller_protocol", detail=str(exc)) from exc
    # A same-key receipt (a previous attempt or flush already committed) can
    # still be pending — heal the wake tail, mirroring `deliver_chat_inbound`.
    if not receipt.inserted and receipt.pending:
        publish_inbound_wake(entry.agent_id, str(receipt.inbound_id))
    return receipt.inbound_id


def _emit(event: str, entry: OutboxEntry, attributes: dict[str, object]) -> None:
    from shared import telemetry

    try:
        telemetry.emit(
            "telemetry",
            event,
            level="warning" if event == "delivery_outbox_abandoned" else "info",
            agent_id=entry.agent_id,
            source="system",
            attributes=attributes,
        )
    except Exception:
        logger.opt(exception=True).warning("[delivery-outbox] {} emit failed", event)


def _abandon(
    path: Path, entry: OutboxEntry, reason: str, moment: datetime, detail: str | None = None
) -> None:
    updated = replace(
        entry,
        state="abandoned",
        abandon_reason=reason,
        abandon_detail=detail,
        abandoned_at=_iso(moment),
    )
    with suppress(OSError):
        _write_atomic(path, updated)
    logger.warning(
        "[delivery-outbox] abandoned delivery to agent {} (source {!r}, {} send attempt(s), "
        "{} flush attempt(s), reason {}{}); record kept at {}",
        entry.agent_id,
        entry.source,
        entry.attempts,
        entry.flush_attempts,
        reason,
        f", detail {detail!r}" if detail else "",
        path,
    )
    _emit(
        "delivery_outbox_abandoned",
        entry,
        {
            "reason": reason,
            "detail": detail,
            "attempts": entry.attempts,
            "flush_attempts": entry.flush_attempts,
            "age_s": max(0.0, (moment - _parse_iso(entry.created_at)).total_seconds()),
            "origin_agent_id": entry.origin_agent_id,
        },
    )


def _due_at(entry: OutboxEntry, steps: tuple[float, ...]) -> datetime:
    base = _parse_iso(entry.last_flush_at or entry.created_at)
    step = steps[min(entry.flush_attempts, len(steps) - 1)]
    return base + timedelta(seconds=step)


def _abandoned_expired(entry: OutboxEntry, moment: datetime, retention_days: int) -> bool:
    """True once an abandoned record's inspection window has elapsed."""
    if entry.abandoned_at is None:
        return False
    return (moment - _parse_iso(entry.abandoned_at)).total_seconds() >= retention_days * 86400


def _record_failed_flush(path: Path, entry: OutboxEntry, moment: datetime) -> OutboxEntry:
    """Persist one failed flush attempt; returns the updated entry."""
    updated = replace(
        entry,
        flush_attempts=entry.flush_attempts + 1,
        last_flush_at=_iso(moment),
    )
    with suppress(OSError):
        _write_atomic(path, updated)
    return updated


def flush(pool: FlushPool, *, now: datetime | None = None) -> FlushReport:
    """One redelivery pass over this machine's records.

    A record is delivered through `insert_chat_inbound_once` (idempotent by its
    stored key), retired on success, retried per the backoff ladder while
    transiently failing, and abandoned — loudly — on a permanent failure or on
    the first failed attempt at or after its budget. The budget decision sits
    after the attempt, never before it: an entry owed a retry at budget time
    still gets it (a flusher stalled across the budget gives the message its
    chance once services return), and a successful attempt delivers at any age.
    An abandoned record is also expired — this pass prunes it — once
    `delivery_outbox_abandoned_retention_days` have elapsed since abandonment.
    A file that fails to parse (bad JSON, drifted schema, unparseable
    timestamps) is counted unreadable and kept for inspection; it never stops
    the pass. While the outbox is disabled, nothing is touched and records
    stay for a re-enable or the operator.
    """
    snapshot = limits()
    moment = now or datetime.now(UTC)
    directory = journal_dir()
    if not directory.is_dir():
        return FlushReport()
    delivered = abandoned = deferred = unreadable = expired = 0
    for path in sorted(directory.iterdir()):
        if path.suffix != _ENTRY_SUFFIX or not path.is_file():
            continue
        entry = _read(path)
        if entry is None:
            unreadable += 1
            logger.warning("[delivery-outbox] unreadable record kept for inspection: {}", path)
            continue
        if entry.state != "pending":
            # Abandoned records keep their inspection window, then this pass
            # expires them (no-op while the outbox is off — the switch is
            # inert, never destructive).
            if snapshot.enabled and _abandoned_expired(
                entry, moment, snapshot.abandoned_retention_days
            ):
                with suppress(OSError):
                    path.unlink()
                expired += 1
            continue
        if not snapshot.enabled:
            deferred += 1
            continue
        age_s = (moment - _parse_iso(entry.created_at)).total_seconds()
        if moment < _due_at(entry, snapshot.retry_backoff_steps):
            deferred += 1
            continue
        try:
            inbound_id = _deliver(pool, entry, snapshot.flush_interval_seconds)
        except PermanentDeliveryError as exc:
            _abandon(path, entry, exc.reason, moment, detail=exc.detail)
            abandoned += 1
        except Exception as exc:
            logger.opt(exception=True).warning(
                "[delivery-outbox] flush attempt for agent {} failed; record kept: {}",
                entry.agent_id,
                path,
            )
            updated = _record_failed_flush(path, entry, moment)
            if age_s >= snapshot.budget_seconds:
                # The failed attempt at/after the budget is the terminal one —
                # abandon it with the attempt already on the record. The
                # decision falls after the attempt, never before it.
                _abandon(path, updated, "budget", moment, detail=str(exc) or None)
                abandoned += 1
            else:
                deferred += 1
        else:
            with suppress(OSError):
                path.unlink()
            delivered += 1
            logger.info(
                "[delivery-outbox] redelivered message to agent {} (source {!r}, "
                "{} send attempt(s)) as inbound {}",
                entry.agent_id,
                entry.source,
                entry.attempts,
                inbound_id,
            )
            _emit(
                "delivery_outbox_flushed",
                entry,
                {
                    "inbound_id": inbound_id,
                    "attempts": entry.attempts,
                    "flush_attempts": entry.flush_attempts,
                    "age_s": max(0.0, age_s),
                    "origin_agent_id": entry.origin_agent_id,
                },
            )
    return FlushReport(
        delivered=delivered,
        abandoned=abandoned,
        deferred=deferred,
        unreadable=unreadable,
        expired=expired,
    )
